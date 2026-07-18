"""Summer surface-environment rasters for Maryland + Washington, DC.

Python port of ``gee/summer_environmental_rasters.js``, using the Earth Engine
Python API (``earthengine-api``) plus ``geemap`` for local downloads. Produces a
co-registered stack of daytime summer environmental variables used as covariates
in the green-gap affordable-housing study:

    LST     Land Surface Temperature (deg C, median)
    NDVI    Normalized Difference Vegetation Index
    NDBI    Normalized Difference Built-up Index
    NDWI    Normalized Difference Water Index (QA / optional water mask)
    ALBEDO  Broadband shortwave albedo (Liang 2001)

Source: Landsat 8 & 9 Collection 2 Level-2 (surface reflectance + surface
temperature), USGS. A 3-year June-August climatology (median of per-scene
indices) damps the effect of any single anomalous summer.

Authentication
--------------
Earth Engine needs a one-time browser auth **you** must run (it cannot be
completed non-interactively here)::

    uv run earthengine authenticate

and a Google Cloud project with the Earth Engine API enabled. Pass it via
``--project`` or the ``EARTHENGINE_PROJECT`` environment variable (read from
``.env``).

Run
---
    # export the analysis-ready stack to Google Drive (folder GEE_green_gap)
    uv run python -m greengap.gee export-drive --project my-ee-project

    # download GeoTIFFs straight into data/external/gee/
    uv run python -m greengap.gee download-local --project my-ee-project

    # just print the config + contributing scene count (cheap sanity check)
    uv run python -m greengap.gee info --project my-ee-project
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path

from loguru import logger
import typer

from greengap.config import EXTERNAL_DATA_DIR

app = typer.Typer(help=__doc__, no_args_is_help=True)


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """All tunables for the summer environmental-raster workflow.

    Mirrors the ``CONFIG`` object in the JavaScript version so the two stay in
    sync. Change the study window here if the housing snapshot moves.
    """

    # Study period: +/-1 year around the ~2022 housing snapshot; Jun-Aug only.
    start_year: int = 2021
    end_year: int = 2023
    summer_start_month: int = 6  # June
    summer_end_month: int = 8  # August

    # Scene-level quality gate; the pixel mask does the fine-grained work.
    max_scene_cloud_cover: int = 60  # percent

    # Export grid, shared by every layer so outputs are pixel-aligned.
    scale: int = 30  # metres
    crs: str = "EPSG:26918"  # UTM 18N, covers MD + DC
    max_pixels: float = 1e13
    drive_folder: str = "GEE_green_gap"

    # Bands carried into the composite.
    analysis_bands: tuple[str, ...] = ("LST", "NDVI", "NDBI", "NDWI", "ALBEDO")

    def __post_init__(self) -> None:
        # filterDate end is exclusive -> extend to the following Jan 1.
        self.start_date = f"{self.start_year}-01-01"
        self.end_date = f"{self.end_year + 1}-01-01"
        self.tag = f"MD_DC_summer_{self.start_year}_{self.end_year}"


# Collection 2 Level-2 scale factors (USGS).
_SR_MULT, _SR_ADD = 0.0000275, -0.2  # surface reflectance
_ST_MULT, _ST_ADD = 0.00341802, 149.0  # surface temperature (Kelvin)


# --------------------------------------------------------------------------- #
# Earth Engine session                                                        #
# --------------------------------------------------------------------------- #
def initialize(project: str | None = None):
    """Initialize Earth Engine, returning the ``ee`` module.

    Import is local so the rest of the package (and the test suite) does not
    require ``earthengine-api`` unless a GEE command is actually invoked.
    """
    import ee

    project = project or os.getenv("EARTHENGINE_PROJECT")
    try:
        ee.Initialize(project=project)
    except Exception as exc:  # not yet authenticated / no project
        raise RuntimeError(
            "Earth Engine failed to initialize. Run `uv run earthengine "
            "authenticate` once, and pass a Cloud project via --project or the "
            "EARTHENGINE_PROJECT env var. Original error: "
            f"{exc}"
        ) from exc
    logger.info(f"Earth Engine initialized (project={project or 'default'}).")
    return ee


# --------------------------------------------------------------------------- #
# Study area                                                                  #
# --------------------------------------------------------------------------- #
def study_area(ee):
    """Dissolved Maryland + DC geometry from the TIGER 2018 states layer."""
    states = ee.FeatureCollection("TIGER/2018/States")
    md = states.filter(ee.Filter.eq("NAME", "Maryland"))
    dc = states.filter(ee.Filter.eq("NAME", "District of Columbia"))
    return md.merge(dc).union().first().geometry()


# --------------------------------------------------------------------------- #
# Per-scene processing                                                         #
# --------------------------------------------------------------------------- #
def _scale_and_rename(image):
    """Apply C2 L2 scale/offset; rename SR bands to common names; ST -> deg C."""
    import ee

    sr = (
        image.select(["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"])
        .multiply(_SR_MULT)
        .add(_SR_ADD)
        .rename(["blue", "green", "red", "nir", "swir1", "swir2"])
        .clamp(0, 1)  # keep reflectance physically plausible
    )
    st = (
        image.select("ST_B10")
        .multiply(_ST_MULT)
        .add(_ST_ADD)  # -> Kelvin
        .subtract(273.15)  # -> Celsius
        .rename("LST")
    )
    # copyProperties returns an ee.Element, but a function mapped over an
    # ImageCollection must return an ee.Image - without this cast the map fails
    # ("mapped function's return value must be an Image") or breaks on the next
    # .select(). Same reason the JS version wraps its result.
    return ee.Image(
        image.addBands(sr, None, True)
        .addBands(st, None, True)
        .copyProperties(image, image.propertyNames())
    )


def _mask_clouds(image):
    """QA_PIXEL + QA_RADSAT mask: cloud, shadow, cirrus, dilated, saturated."""
    qa = image.select("QA_PIXEL")
    dilated, cirrus, cloud, shadow = 1 << 1, 1 << 2, 1 << 3, 1 << 4
    qa_mask = (
        qa.bitwiseAnd(dilated)
        .eq(0)
        .And(qa.bitwiseAnd(cirrus).eq(0))
        .And(qa.bitwiseAnd(cloud).eq(0))
        .And(qa.bitwiseAnd(shadow).eq(0))
    )
    sat_mask = image.select("QA_RADSAT").eq(0)
    return image.updateMask(qa_mask).updateMask(sat_mask)


def _add_indices(image):
    """Per-scene NDVI, NDBI, NDWI and broadband albedo (Liang 2001)."""
    ndvi = image.normalizedDifference(["nir", "red"]).rename("NDVI")
    ndbi = image.normalizedDifference(["swir1", "nir"]).rename("NDBI")
    ndwi = image.normalizedDifference(["green", "nir"]).rename("NDWI")
    albedo = image.expression(
        "0.356*blue + 0.130*red + 0.373*nir + 0.085*swir1 + 0.072*swir2 - 0.0018",
        {
            "blue": image.select("blue"),
            "red": image.select("red"),
            "nir": image.select("nir"),
            "swir1": image.select("swir1"),
            "swir2": image.select("swir2"),
        },
    ).rename("ALBEDO")
    return image.addBands([ndvi, ndbi, ndwi, albedo])


# --------------------------------------------------------------------------- #
# Collection + composite                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class Products:
    """Bundle of the EE objects a run produces (kept lazy / server-side)."""

    composite: object
    clear_count: object
    region: object
    n_scenes: object
    bands: tuple[str, ...] = field(default_factory=tuple)


def build_products(ee, cfg: Config) -> Products:
    """Assemble the masked, scaled, per-scene-indexed summer median composite."""
    region = study_area(ee)

    def summer_filter(collection):
        return (
            collection.filterDate(cfg.start_date, cfg.end_date)
            .filter(
                ee.Filter.calendarRange(
                    cfg.summer_start_month, cfg.summer_end_month, "month"
                )
            )
            .filter(ee.Filter.lt("CLOUD_COVER", cfg.max_scene_cloud_cover))
            .filterBounds(region)
        )

    l8 = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
    l9 = ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")

    merged = (
        summer_filter(l8.merge(l9))
        .map(_mask_clouds)  # 1. drop cloud/shadow/saturated pixels
        .map(_scale_and_rename)  # 2. physical units + common band names
        .map(_add_indices)  # 3. per-scene indices
    )

    analysis = merged.select(list(cfg.analysis_bands))

    composite = (
        analysis.median()  # median over 3 summers of per-scene indices
        .clip(region)
        .set(
            {
                "system:time_start": ee.Date(cfg.start_date).millis(),
                "study_tag": cfg.tag,
                "source": "Landsat 8/9 C02 L2 (LC08_L2, LC09_L2)",
                "period": f"{cfg.start_year}-{cfg.end_year} (Jun-Aug)",
                "reducer": "median",
                "scene_cloud_max": cfg.max_scene_cloud_cover,
                "scale_m": cfg.scale,
                "crs": cfg.crs,
                "albedo_method": "Liang (2001) narrow-to-broadband",
            }
        )
    )

    clear_count = (
        analysis.select("LST")
        .count()
        .rename("CLEAR_OBS")
        .clip(region)
        .toInt16()
    )

    return Products(
        composite=composite,
        clear_count=clear_count,
        region=region,
        n_scenes=merged.size(),
        bands=cfg.analysis_bands,
    )


def manifest_dict(cfg: Config, n_scenes: int) -> dict:
    """Machine-readable provenance sidecar (written next to the rasters)."""
    from datetime import datetime, timezone

    return {
        "study_tag": cfg.tag,
        "source": "Landsat 8/9 Collection 2 Level-2",
        "collections": "LANDSAT/LC08/C02/T1_L2 ; LANDSAT/LC09/C02/T1_L2",
        "period": f"{cfg.start_year}-{cfg.end_year}",
        "months": "Jun-Aug",
        "reducer": "median (per-scene indices)",
        "scene_cloud_max": cfg.max_scene_cloud_cover,
        "n_scenes": n_scenes,
        "bands": list(cfg.analysis_bands),
        "scale_m": cfg.scale,
        "crs": cfg.crs,
        "albedo_method": "Liang 2001 narrow-to-broadband",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------- #
# CLI commands                                                                #
# --------------------------------------------------------------------------- #
@app.command()
def info(project: str = typer.Option(None, help="Google Cloud project for EE.")):
    """Print the config and the number of contributing Landsat scenes."""
    cfg = Config()
    ee = initialize(project)
    products = build_products(ee, cfg)

    logger.info(f"Study tag:        {cfg.tag}")
    logger.info(f"Period:           {cfg.start_year}-{cfg.end_year} (Jun-Aug)")
    logger.info(f"Scene cloud (<):  {cfg.max_scene_cloud_cover}%")
    logger.info(f"Analysis bands:   {', '.join(cfg.analysis_bands)}")
    logger.info(f"Export grid:      {cfg.scale} m, {cfg.crs}")
    n = products.n_scenes.getInfo()  # server round-trip
    logger.success(f"Contributing Landsat scenes: {n}")


@app.command("export-drive")
def export_drive(
    project: str = typer.Option(None, help="Google Cloud project for EE."),
    stack: bool = typer.Option(True, help="Export the multi-band stack."),
    per_layer: bool = typer.Option(True, help="Export one GeoTIFF per band."),
    clear_obs: bool = typer.Option(True, help="Export the clear-obs QA layer."),
):
    """Queue GeoTIFF exports to Google Drive (mirrors the JS Export tasks)."""
    cfg = Config()
    ee = initialize(project)
    p = build_products(ee, cfg)

    common = dict(
        region=p.region,
        scale=cfg.scale,
        crs=cfg.crs,
        maxPixels=cfg.max_pixels,
        folder=cfg.drive_folder,
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True},
    )
    tasks = []

    def start(image, name):
        task = ee.batch.Export.image.toDrive(
            image=image, description=name, fileNamePrefix=name, **common
        )
        task.start()
        tasks.append(name)
        logger.info(f"Started export task: {name}")

    if stack:
        start(p.composite.toFloat(), f"{cfg.tag}_stack")
    if per_layer:
        for band in cfg.analysis_bands:
            start(p.composite.select(band).toFloat(), f"{cfg.tag}_{band}")
    if clear_obs:
        start(p.clear_count, f"{cfg.tag}_CLEAR_OBS")

    _write_manifest(cfg, p.n_scenes.getInfo())
    logger.success(
        f"Queued {len(tasks)} Drive export task(s) -> folder '{cfg.drive_folder}'. "
        "Monitor at https://code.earthengine.google.com/tasks"
    )


@app.command("download-local")
def download_local(
    project: str = typer.Option(None, help="Google Cloud project for EE."),
    out_dir: Path = typer.Option(
        EXTERNAL_DATA_DIR / "gee",
        help="Destination folder for GeoTIFFs (default data/external/gee/).",
    ),
    stack_only: bool = typer.Option(
        False, help="Download only the multi-band stack (skip per-layer files)."
    ),
):
    """Download GeoTIFFs straight into the repo (no Google Drive, no Drive scope).

    Uses ``geemap.download_ee_image`` (geedim backend), which **tiles and
    stitches** automatically. This matters: MD+DC at 30 m is ~88 M pixels/band,
    and the 5-band stack is ~1.8 GB - roughly 37x over Earth Engine's ~48 MB
    direct-download cap, so the simpler ``ee_export_image`` / ``getDownloadURL``
    path would fail outright. geedim splits the request into tiles under the cap
    and reassembles a single GeoTIFF.
    """
    import geemap

    cfg = Config()
    ee = initialize(project)
    p = build_products(ee, cfg)

    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading to {out_dir} (tiled via geedim; large area, be patient)")

    def grab(image, name):
        dest = out_dir / f"{name}.tif"
        if dest.exists():
            logger.info(f"  skip {dest.name} (exists)")
            return
        logger.info(f"  -> {dest.name}")
        # crs_transform is not passed, so geedim derives the grid from crs+scale;
        # every band uses the identical crs/scale/region so the files stay aligned.
        geemap.download_ee_image(
            image,
            filename=str(dest),
            region=p.region,
            crs=cfg.crs,
            scale=cfg.scale,
        )

    grab(p.composite.toFloat(), f"{cfg.tag}_stack")
    if not stack_only:
        for band in cfg.analysis_bands:
            grab(p.composite.select(band).toFloat(), f"{cfg.tag}_{band}")
        grab(p.clear_count, f"{cfg.tag}_CLEAR_OBS")

    _write_manifest(cfg, p.n_scenes.getInfo(), out_dir=out_dir)
    logger.success(f"Done. Rasters + manifest in {out_dir}")


def _write_manifest(cfg: Config, n_scenes: int, out_dir: Path | None = None) -> Path:
    """Write the provenance JSON next to the rasters (always local)."""
    out_dir = out_dir or (EXTERNAL_DATA_DIR / "gee")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{cfg.tag}_manifest.json"
    path.write_text(json.dumps(manifest_dict(cfg, n_scenes), indent=2))
    logger.info(f"Wrote manifest: {path}")
    return path


if __name__ == "__main__":
    app()

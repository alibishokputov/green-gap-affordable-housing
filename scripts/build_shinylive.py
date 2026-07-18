"""Compile the Shiny dashboard to static WebAssembly for GitHub Pages.

``shinylive`` bundles Pyodide + the app into plain files that any static host can
serve - no Python server, no runtime limits, and nothing of ours to keep awake.

Assembles a self-contained app directory and exports it::

    uv run python scripts/build_shinylive.py --out _site/dashboard

Two Pyodide constraints shape this script:

1. **No pyarrow.** ``read_parquet`` cannot work in the browser, so the analysis
   table is converted to GeoJSON here and read back with
   ``GeoDataFrame.from_features`` (shapely only - no pyarrow, no GDAL).
2. **No dependency resolution.** shinylive installs exactly what
   ``requirements.txt`` names, and nothing it depends on. folium needs branca,
   jinja2, xyzservices and requests; mapclassify needs networkx and
   scikit-learn. Omit any one and the app dies at import with a bare
   ModuleNotFoundError in the browser.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import geopandas as gpd

PROJ_ROOT = Path(__file__).resolve().parents[1]
APP_SRC = PROJ_ROOT / "app" / "app.py"
# Vendored next to the app so it keeps one source of truth with the package while
# still importing cleanly in Pyodide, where `greengap` is not installed.
VENDORED = [
    PROJ_ROOT / "greengap" / "bivariate.py",
    PROJ_ROOT / "greengap" / "classify.py",
]
# The dashboard ships BOTH geographies (block group + tract) and toggles between
# them in-app. Each has an analysis parquet (local) and a committed GeoJSON (the
# hand-off to the published site). These GeoJSONs are committed on purpose: CI
# cannot rebuild them - that needs the 2.4 GB rasters and a ~25 min extraction.
# app.py reads these exact filenames (GEOG_FILES). Regenerate with --refresh-data
# and commit the result.
GEOGS = {
    "bg": {
        "analysis": PROJ_ROOT / "data" / "processed" / "bg_analysis.parquet",
        "geojson": PROJ_ROOT / "app" / "units_bg.geojson",
    },
    "tract": {
        "analysis": PROJ_ROOT / "data" / "processed" / "tract_analysis.parquet",
        "geojson": PROJ_ROOT / "app" / "units_tract.geojson",
    },
}

# Every module the app imports, plus their transitive deps: shinylive installs
# exactly this list and resolves nothing itself.
# geopandas/shapely/pyproj/scipy/numpy/pandas ship with Pyodide already.
REQUIREMENTS = [
    "folium",
    "branca",  # folium dep
    "jinja2",  # folium dep
    "xyzservices",  # folium dep
    "requests",  # folium.features imports it (never actually calls out)
]

# Deliberately NOT listed, and load-bearing:
#
#   matplotlib  - importing it under Pyodide starts a font-cache build that never
#                 completes (measured >330 s with no first paint - a hang, not
#                 slowness). Every chart is HTML/CSS or inline SVG instead.
#   mapclassify - imports matplotlib at module scope, so it triggers exactly the
#                 same hang. greengap/classify.py replaces the three schemes we
#                 need using numpy only; tests/test_classify.py pins it to
#                 mapclassify's results.
#   scikit-learn / networkx - only ever needed by mapclassify.
#
# Re-measure first paint before adding any of these back.

# Columns the dashboard actually uses. Everything else (raw class fractions,
# ALAND/AWATER, coverage diagnostics) stays out of the browser payload.
KEEP_COLS = [
    "GEOID",
    "NAMELSAD",
    "county",
    "canopy_pct",
    "natural_canopy_pct",
    "canopy_pct_total",
    "mean_lst",
    "mean_ndvi",
    "mean_ndbi",
    "lihtc_units_low_income",
    "lihtc_units_total",
    "lihtc_projects",
    "geometry",
]
ROUND_COLS = ["canopy_pct", "natural_canopy_pct", "canopy_pct_total",
              "mean_lst", "mean_ndvi", "mean_ndbi"]

SIMPLIFY_TOLERANCE = 0.0003  # ~30 m; display only


def build_geojson(analysis: Path, dest: Path) -> tuple[int, int]:
    """Write the slimmed, simplified GeoJSON the browser app reads."""
    if not analysis.exists():
        sys.exit(
            f"Analysis table missing: {analysis}\n"
            "Build it first:  uv run python -m greengap.dataset build --geog "
            f"{'bg' if 'bg' in analysis.name else 'tract'}"
        )
    gdf = gpd.read_parquet(analysis)

    # Keep every unit: canopy-unreliable ones (open water / military nodata) still
    # carry valid LST/NDVI, and the app excludes NaN per-variable. build sets their
    # canopy_* to NaN already, which GeoJSON serialises as null.
    n_unmeasured = int((~gdf["canopy_reliable"]).sum())

    gdf = gdf[KEEP_COLS].copy()
    for col in ROUND_COLS:
        gdf[col] = gdf[col].astype(float).round(2)

    gdf["geometry"] = gdf.geometry.simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
    # Simplification can leave self-intersecting rings. Any GEOS op on those
    # (union, overlay) raises TopologyException, which is a *fatal* crash of the
    # WebAssembly runtime rather than a catchable Python error - so repair here,
    # where a failure is a build error we can see, not a white page for a reader.
    invalid = ~gdf.geometry.is_valid
    if invalid.any():
        print(f"repairing {int(invalid.sum())} invalid geometr(ies) after simplification")
        gdf.loc[invalid, "geometry"] = gdf.loc[invalid, "geometry"].make_valid()
    still_bad = int((~gdf.geometry.is_valid).sum())
    if still_bad:
        sys.exit(f"{still_bad} geometries remain invalid after make_valid(); aborting")

    payload = json.loads(gdf.to_json())
    # Carry the unmeasured-canopy count through so the app can report it honestly.
    payload["unmeasured_canopy"] = n_unmeasured
    dest.write_text(json.dumps(payload, separators=(",", ":")))
    return len(gdf), n_unmeasured


def _assert_no_matplotlib(staging: Path) -> None:
    """Fail the build if importing the staged app pulls in matplotlib.

    This is the invariant the static build depends on, and it is easy to break
    from a distance: mapclassify imported matplotlib at module scope, so a single
    innocuous-looking import re-introduces a hang that only shows up as a blank
    page in the browser several minutes later. Catch it here instead.
    """
    probe = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(staging)!r})
        import app  # noqa: F401
        hits = sorted({{m.split('.')[0] for m in sys.modules if m.startswith('matplotlib')}})
        print(','.join(hits))
    """)
    # Run in a clean interpreter: this process has already imported matplotlib
    # via other project modules, so an in-process check would be meaningless.
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=PROJ_ROOT,
    )
    if result.returncode != 0:
        sys.exit(f"staged app failed to import:\n{result.stderr[-1500:]}")
    hits = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if hits:
        sys.exit(
            f"ABORT: the staged app imports matplotlib ({hits}).\n"
            "Under Pyodide this starts a font-cache build that never finishes, so the "
            "published dashboard would hang on a blank page. Find the offending import "
            "(mapclassify is a known one) and remove it."
        )
    print("checked: staged app imports no matplotlib")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJ_ROOT / "_site" / "dashboard",
        help="Output directory for the static site (default: _site/dashboard).",
    )
    parser.add_argument(
        "--staging",
        type=Path,
        default=PROJ_ROOT / "build" / "shinylive_app",
        help="Where to assemble the self-contained app before exporting.",
    )
    parser.add_argument(
        "--refresh-data",
        action="store_true",
        help="Regenerate each geography's GeoJSON from its analysis table, then commit.",
    )
    args = parser.parse_args()

    # Regenerate each geography's GeoJSON when asked, or when it is missing but its
    # analysis table exists. A geography with neither is simply skipped (the app
    # loads whichever GeoJSONs are present).
    for geog, files in GEOGS.items():
        gj, an = files["geojson"], files["analysis"]
        if args.refresh_data or (not gj.exists() and an.exists()):
            n, n_unmeasured = build_geojson(an, gj)
            print(
                f"regenerated {gj.relative_to(PROJ_ROOT)}: {n} {geog} units "
                f"({n_unmeasured} unmeasured canopy), {gj.stat().st_size / 1e6:.1f} MB"
            )
        elif gj.exists():
            print(f"using committed {gj.relative_to(PROJ_ROOT)} (--refresh-data to rebuild)")

    present = [g for g, f in GEOGS.items() if f["geojson"].exists()]
    if not present:
        sys.exit("No geography GeoJSONs present. Run with --refresh-data after building a table.")

    staging: Path = args.staging
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    shutil.copy2(APP_SRC, staging / "app.py")
    for src in VENDORED:
        shutil.copy2(src, staging / src.name)
    for geog in present:
        shutil.copy2(GEOGS[geog]["geojson"], staging / GEOGS[geog]["geojson"].name)
    (staging / "requirements.txt").write_text("\n".join(REQUIREMENTS) + "\n")

    # Guard the invariant the whole static build rests on: nothing the app
    # imports may drag in matplotlib, or the published page hangs on load.
    _assert_no_matplotlib(staging)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    # shinylive ships a console script, not a __main__ module, so `-m` fails.
    # Prefer the one next to the running interpreter (i.e. inside this venv).
    exe = Path(sys.executable).parent / "shinylive"
    cmd = [str(exe)] if exe.exists() else [shutil.which("shinylive") or "shinylive"]
    subprocess.run([*cmd, "export", str(staging), str(out)], check=True)

    print(f"exported static dashboard -> {out}")
    print(f"preview:  python3 -m http.server --directory {out} 8000")


if __name__ == "__main__":
    main()

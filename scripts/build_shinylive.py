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
ANALYSIS = PROJ_ROOT / "data" / "processed" / "tract_canopy_lihtc.parquet"

# The dashboard's data is committed, unlike everything else under data/ (which is
# git-ignored). CI cannot rebuild it: that needs the 2.4 GB Chesapeake rasters and
# a ~20-minute extraction. So this small derived file is the tracked hand-off
# between the local pipeline and the published site. Regenerate it with
# --refresh-data after rebuilding the analysis table, and commit the result.
GEOJSON_SRC = PROJ_ROOT / "app" / "tracts.geojson"

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
    "lihtc_units_low_income",
    "lihtc_units_total",
    "lihtc_projects",
    "geometry",
]

SIMPLIFY_TOLERANCE = 0.0003  # ~30 m; display only


def build_geojson(dest: Path) -> tuple[int, int]:
    """Write the slimmed, simplified GeoJSON the browser app reads."""
    if not ANALYSIS.exists():
        sys.exit(
            f"Analysis table missing: {ANALYSIS}\n"
            "Build it first:  uv run python -m greengap.dataset build"
        )
    gdf = gpd.read_parquet(ANALYSIS)

    n_unmeasured = int((~gdf["canopy_reliable"]).sum())
    gdf = gdf[gdf["canopy_reliable"]].copy()

    gdf = gdf[KEEP_COLS].copy()
    for col in ("canopy_pct", "natural_canopy_pct", "canopy_pct_total"):
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
    # Carry the excluded-tract count through so the app can report it honestly.
    payload["unmeasured_tracts"] = n_unmeasured
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
        help=f"Regenerate {GEOJSON_SRC.name} from the analysis table, then commit it.",
    )
    args = parser.parse_args()

    if args.refresh_data or not GEOJSON_SRC.exists():
        n, n_unmeasured = build_geojson(GEOJSON_SRC)
        size_mb = GEOJSON_SRC.stat().st_size / 1e6
        print(
            f"regenerated {GEOJSON_SRC.relative_to(PROJ_ROOT)}: {n} tracts "
            f"({n_unmeasured} unmeasurable excluded), {size_mb:.1f} MB - commit this file"
        )
    else:
        print(f"using committed {GEOJSON_SRC.relative_to(PROJ_ROOT)} (--refresh-data to rebuild)")

    staging: Path = args.staging
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    shutil.copy2(APP_SRC, staging / "app.py")
    for src in VENDORED:
        shutil.copy2(src, staging / src.name)
    shutil.copy2(GEOJSON_SRC, staging / "tracts.geojson")
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

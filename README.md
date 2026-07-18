# Green Gap Affordable Housing

<a target="_blank" href="https://cookiecutter-data-science.drivendata.org/">
    <img src="https://img.shields.io/badge/CCDS-Project%20template-328F97?logo=cookiecutter" />
</a>

Spatial analysis of the **green gap** in affordable housing across Washington, DC and
Maryland — how green space (from land cover / land use rasters) relates to the location
of affordable housing (LIHTC and NHPD).

## Stack

- **Environment:** [uv](https://docs.astral.sh/uv/) (Python 3.12)
- **Vector geospatial:** geopandas, shapely, pyogrio, pyproj, mapclassify, libpysal, esda
- **Raster / satellite:** rasterio, rioxarray, xarray, dask, rasterstats, geocube
- **Basemaps:** contextily
- **Static viz:** matplotlib, **plotnine** (grammar of graphics)
- **Interactive maps:** folium, plotly, leafmap (+ a **bivariate choropleth** helper)
- **Notebooks → public site:** Jupyter + **Quarto**, published to **GitHub Pages** via GitHub Actions

## Quickstart

```bash
# 1. Install the environment (creates .venv, resolves & locks everything)
uv sync

# 2. Register / launch Jupyter Lab
uv run jupyter lab

# 3. Preview the public site locally (renders notebooks with live maps)
uv run quarto preview

# 4. Run tests & lint
uv run pytest
uv run ruff check
```

## Publishing the notebooks (public site)

Notebooks in `notebooks/` are rendered by Quarto into a static website and published to
GitHub Pages automatically on every push to `main` (see `.github/workflows/publish.yml`).

**One-time setup on GitHub:** repo *Settings → Pages → Build and deployment → Source =
GitHub Actions*. After the first successful run the site is live at
`https://<user>.github.io/green-gap-affordable-housing/`.

Executed notebook outputs are cached in `_freeze/` (committed) via Quarto's
`freeze: auto`, so CI does not re-run heavy geospatial code unless a notebook changes.
When you edit a notebook, run `uv run quarto render` locally once to refresh the freeze,
then commit the updated `_freeze/`.

## Bivariate maps

`greengap/bivariate.py` provides a small toolkit for **bivariate choropleths** (encoding
two variables at once on a 3×3 color grid) that works with folium, geopandas `.explore`,
matplotlib, or plotly. See `notebooks/1.0-example-green-gap-demo.ipynb` for a worked
example crossing *green-space share* × *affordable-housing units*.

## Green-gap dashboard (Shiny)

An interactive bivariate map crossing a **summer-environment measure** (surface temperature,
tree canopy %, NDVI, or NDBI) against **LIHTC affordable units**, at **census block-group**
level for **Maryland + Washington, DC** (~4,650 block groups).

**Published (no server, no install):** `https://<user>.github.io/green-gap-affordable-housing/dashboard/`

Run it locally:

```bash
uv run python -m greengap.dataset build   # one-time: builds the analysis table (~25 min)
uv run shiny run app/app.py               # then open http://127.0.0.1:8000
```

The vertical axis is selectable; **surface temperature (LST)** is the default. The "gap" cell —
the worst environment carrying the most affordable housing — flips corner with the variable's
polarity (low canopy vs. *high* heat), computed from `ENV_VARS[...]["worse"]`, not hard-coded.

### How it's published

`scripts/build_shinylive.py` compiles the app to **WebAssembly** (shinylive), so it runs
entirely in the reader's browser and is served as static files from GitHub Pages — no server,
no runtime limits, nothing to keep awake. The Pages workflow builds it into `_site/dashboard`
on every push to `main`.

```bash
# rebuild the dashboard's data after re-running the pipeline, then commit it
uv run python scripts/build_shinylive.py --refresh-data --out _site/dashboard
python3 -m http.server --directory _site/dashboard 8000   # preview the static build
```

`app/units.geojson` is **committed on purpose** — it is the hand-off between the local pipeline
and the published site, because CI cannot rebuild it (that needs the 2.4 GB rasters and a
~25-minute extraction). Regenerate it with `--refresh-data` whenever the analysis table changes,
and commit the result.

> ### Pyodide constraints — read before editing `app/app.py`
>
> The app runs in the browser, which rules out things that work fine locally. These are not
> style preferences; each one was a blank page:
>
> - **No `matplotlib`.** Importing it starts a font-cache build that never completes (measured
>   >330 s with no first paint). Every chart is HTML/CSS or inline SVG instead.
> - **No `mapclassify`** — it imports matplotlib at module scope, so it triggers the same hang.
>   `greengap/classify.py` reimplements the three schemes with numpy only, and
>   `tests/test_classify.py` pins it to mapclassify's results so the published map and the local
>   map cannot diverge.
> - **No `pyarrow`** → no `read_parquet` in the browser. The app reads bundled GeoJSON via
>   `GeoDataFrame.from_features` (shapely only, no GDAL).
> - **No `greengap.config`** — it pulls in `dotenv`/`loguru`.
> - **No `union_all()`** on the tract geometries: GEOS raises a TopologyException on
>   simplification artefacts, which *hard-crashes* the WASM runtime rather than raising.
> - Declare **every transitive dependency** in the build script's `REQUIREMENTS`; shinylive
>   resolves none of them (folium alone needs branca, jinja2, xyzservices and requests).
>
> The build script asserts the no-matplotlib invariant and fails loudly if it is ever broken.
> First paint is currently ~11 s; re-measure it if you add a dependency.

The dashboard has a bivariate map, a joint-distribution heatmap, a scatter with an OLS fit,
and a filtered table of **gap areas** (worst environment × most affordable housing). Controls:
county filter, environmental measure, LIHTC measure, classification scheme, palette, and whether
breaks are computed on the visible selection or the whole study area.

### The data pipeline

`greengap/dataset.py` builds the analysis table in cached steps. The **unit of analysis** is
parametrized: `--geog bg` (block group, default) or `--geog tract` (coarser robustness check);
all caches are geography-scoped so both coexist.

| Step | What | Output (bg) |
|------|------|-------------|
| `boundaries` | TIGER census units + county names (MD + DC) | `data/interim/boundaries_bg.parquet` |
| `lihtc` | LIHTC points → summed to units | `data/interim/lihtc_by_bg.parquet` |
| `canopy` | 1 m Chesapeake land cover → per-unit class fractions (`exactextract`) | `data/interim/canopy_bg_{24,11}.parquet` |
| `landsat` | Summer LST/NDVI/NDBI/albedo per unit from the GEE stack | `data/interim/landsat_by_bg.parquet` |
| `build` | join of the above | `data/processed/bg_analysis.parquet` |

The `landsat` step needs the Earth Engine export (`greengap/gee.py` → `data/external/gee/`); see
[gee/README.md](gee/README.md). LST is Landsat thermal, natively ~100 m — a block-group mean is
stable, but do not read it as 30 m detail.

Run any step alone (`uv run python -m greengap.dataset canopy --state 11`). The MD raster is
2.4 GB at 1 m, so `canopy` is the slow step (~20 min for MD; DC takes 8 s) — it caches
per-state so you only pay once, and `build --force` deliberately will **not** re-trigger it
(use `--force-upstream` for that). Pass `--raster /path/to/extracted.tif` to skip reading
inside the zip.

**Four things worth knowing about the data:**

- **Use the national LIHTC file, not `LIHTC.csv`.** The bundled CSV is a Maryland-only extract
  (948 MD rows, **zero DC**). The pipeline instead reads HUD's national database from
  `data/raw/lihtc.zip` (`LIHTCPUB.xlsx`), which has MD 948 + **DC 268**. `openpyxl` cannot
  parse it (HUD emits a `synchVertical` attribute it rejects), hence the `calamine` engine.
- **Use HUD's imputed unit columns.** Raw `li_units` has 27 nulls, which `sum()` skips
  silently — a ~2,200-unit undercount. The pipeline uses `li_unitr`/`n_unitsr` (0 nulls), as
  HUD's data dictionary instructs.
- **Canopy definition matters.** The Chesapeake land cover splits canopy across four classes
  (3 = Tree Canopy; 10/11/12 = canopy over structures / other impervious / roads). The default
  `canopy_pct` counts all four (standard urban-tree-canopy practice) and divides by
  **classified land area** (water *and* nodata excluded). Counting only class 3 undercounts
  canopy by up to ~3× in dense urban areas — exactly where LIHTC concentrates. Both definitions
  are in the app so you can test sensitivity.
- **Some units have no measurable canopy** — open-water units (Census `99xxxx` series) and
  military land, which the Chesapeake raster leaves as nodata (Aberdeen Proving Ground, Joint
  Base Andrews). Their `canopy_*` is set to NaN and flagged by `canopy_reliable`; the app
  excludes them from *canopy* views only (they keep valid LST/NDVI). Validation: area-weighted
  canopy ≈ 34.9% for DC (published UTC ≈ 38%; our land cover is the 2017 edition) and 49.8% MD.
- **LST reveals the urban heat island directly.** Summer surface temperature spans ~22–48 °C
  across block groups; dense urban cores run 8–9 °C hotter than vegetated fringes. That gradient
  is the outcome the study tests — is affordable housing in the hotter block groups, after
  controlling for canopy and built form?

> **Classification gotcha:** most units have zero LIHTC. Quantile breaks therefore put both cut
> points at 0, collapsing the LIHTC axis to two classes and **emptying the high-LIHTC corner the
> study is about**. Natural breaks is the default; the app warns if quantiles degenerate.

## Project organization

```
├── LICENSE
├── Makefile           <- `make requirements`, `make lint`, `make format`, `make test`, `make data`
├── README.md
├── pyproject.toml     <- Dependencies & tool config (managed by uv)
├── uv.lock            <- Locked, reproducible dependency versions
├── .python-version    <- Pins Python 3.12 for uv
│
├── _quarto.yml        <- Quarto website config (renders notebooks → _site/)
├── index.qmd          <- Site landing page
├── _freeze/           <- Cached notebook execution outputs (committed; drives CI)
├── .github/workflows/
│   └── publish.yml    <- Render + deploy to GitHub Pages
│
├── data               <- Not committed (see .gitignore); only the folder scaffold is tracked
│   ├── raw            <- Original, immutable study inputs (parcels, LIHTC, NHPD)
│   ├── external       <- Third-party sources (Chesapeake LULC rasters; Landsat exports under gee/)
│   ├── interim        <- Intermediate transformed data
│   └── processed      <- Final, canonical datasets for analysis
│
├── app                <- Shiny dashboard (bivariate canopy x LIHTC map)
│   ├── app.py           `uv run shiny run app/app.py`; also compiled to WASM
│   └── units.geojson    Committed on purpose: the dashboard's data (CI can't rebuild it)
├── scripts
│   └── build_shinylive.py  <- Compiles the dashboard to static WASM for GitHub Pages
├── gee                <- Google Earth Engine scripts (Landsat summer environmental rasters)
│                         + README; outputs land in data/external/gee/
├── notebooks          <- Analysis notebooks (published to the site). Naming:
│                         <number>-<initials>-<short-description>.ipynb
├── references         <- Data dictionaries, manuals, provenance
├── reports            <- Generated analysis (HTML, PDF, ...)
│   └── figures        <- Generated figures
├── models             <- Trained/serialized models & predictions
│
├── greengap           <- Source package
│   ├── config.py      <- Paths (PROJ_ROOT, RAW_DATA_DIR, ...) + logging
│   ├── dataset.py     <- Tract/LIHTC/canopy pipeline (typer CLI)
│   ├── gee.py         <- Earth Engine workflow in Python (typer CLI)
│   ├── features.py    <- Feature engineering
│   ├── bivariate.py   <- Bivariate choropleth helpers
│   ├── classify.py    <- numpy-only class breaks (mapclassify can't run in the browser)
│   ├── plots.py       <- Figure generation
│   └── modeling/      <- train.py / predict.py
└── tests              <- pytest suite
```

--------
Scaffold based on the [cookiecutter data science](https://cookiecutter-data-science.drivendata.org/) v2 template.

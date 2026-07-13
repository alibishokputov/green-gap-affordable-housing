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
│   ├── raw            <- Original, immutable dumps (parcels, land cover/use rasters, LIHTC, NHPD)
│   ├── external       <- Third-party sources
│   ├── interim        <- Intermediate transformed data
│   └── processed      <- Final, canonical datasets for analysis
│
├── notebooks          <- Analysis notebooks (published to the site). Naming:
│                         <number>-<initials>-<short-description>.ipynb
├── references         <- Data dictionaries, manuals, provenance
├── reports            <- Generated analysis (HTML, PDF, ...)
│   └── figures        <- Generated figures
├── models             <- Trained/serialized models & predictions
│
├── greengap           <- Source package
│   ├── config.py      <- Paths (PROJ_ROOT, RAW_DATA_DIR, ...) + logging
│   ├── dataset.py     <- Data loading / processing entry point
│   ├── features.py    <- Feature engineering
│   ├── bivariate.py   <- Bivariate choropleth helpers
│   ├── plots.py       <- Figure generation
│   └── modeling/      <- train.py / predict.py
└── tests              <- pytest suite
```

--------
Scaffold based on the [cookiecutter data science](https://cookiecutter-data-science.drivendata.org/) v2 template.

# Google Earth Engine scripts

Two equivalent implementations that generate the **satellite-derived environmental rasters**
used as covariates in the green-gap analysis (surface temperature, vegetation, built form,
albedo):

- **`summer_environmental_rasters.js`** — runs in the browser
  [Earth Engine Code Editor](https://code.earthengine.google.com/). Zero local setup.
- **`greengap/gee.py`** — a Python port using `earthengine-api` + `geemap`, runnable from this
  repo (fits the uv/Quarto stack; can download GeoTIFFs straight into `data/external/gee/`).

Both produce identical outputs (same masking, scaling, indices, 3-year climatology, and export
grid). Pick the JS path for a quick one-off; pick the Python path for a reproducible,
version-controlled workflow.

## `summer_environmental_rasters.js`

Builds a co-registered summer-climatology stack for **Maryland + Washington, DC** from
**Landsat 8 & 9 Collection 2 Level-2**:

| Band | Meaning | Notes |
|------|---------|-------|
| `LST` | Land Surface Temperature (°C) | from `ST_B10`, USGS scale factors |
| `NDVI` | Vegetation index | `(NIR − Red) / (NIR + Red)` |
| `NDBI` | Built-up index | `(SWIR1 − NIR) / (SWIR1 + NIR)` |
| `NDWI` | Water index | for QA / optional open-water masking |
| `ALBEDO` | Broadband shortwave albedo | Liang (2001) narrow-to-broadband |
| `CLEAR_OBS` | # clear observations per pixel | reliability layer (exported separately) |

### Why a 3-year climatology?

The default window is **June–August, 2021–2023** (`CONFIG.startYear` / `endYear`). Keeping
only summer months across three years and taking the **median of per-scene indices** yields
a stable estimate of the local thermal/vegetation environment, rather than a snapshot biased
by one anomalous summer. If your housing snapshot moves, re-centre the window in `CONFIG`.

Indices are computed **per scene** (after cloud masking and physical-unit scaling) and only
then reduced with a median — the statistically correct order, versus compositing raw bands
first.

### How to run — browser (JS)

1. Open <https://code.earthengine.google.com/> (needs a Google Earth Engine account).
2. Paste the contents of `summer_environmental_rasters.js` and press **Run**.
3. Review the console: contributing scene count, band names, and the provenance manifest.
4. Open the **Tasks** tab and start each export. Files are written to Google Drive folder
   `GEE_green_gap` (set by `CONFIG.driveFolder`).

### How to run — Python (this repo)

One-time authentication (**you** must do this — it opens a browser and cannot be automated),
plus a Google Cloud project with the Earth Engine API enabled:

```bash
uv run earthengine authenticate
# then pass the project with --project, or set EARTHENGINE_PROJECT in .env
```

Then, from the repo root:

```bash
# cheap sanity check: prints config + how many Landsat scenes contribute
uv run python -m greengap.gee info --project <your-ee-project>

# queue GeoTIFF exports to Google Drive (folder GEE_green_gap) — like the JS
uv run python -m greengap.gee export-drive --project <your-ee-project>

# OR download GeoTIFFs straight into data/external/gee/ (no Drive round-trip)
uv run python -m greengap.gee download-local --project <your-ee-project>
```

The study window, cloud thresholds and export grid live in the `Config` dataclass at the top of
`greengap/gee.py` (the Python mirror of the JS `CONFIG`). Note: `download-local` is subject to
Earth Engine's direct-download size cap; for the full MD+DC stack at 30 m, prefer `export-drive`
if you hit a size error.

### Exports

All exports share one grid — **30 m, `EPSG:26918` (UTM 18N)** — so they are pixel-aligned and
stackable with each other and with the 1 m Chesapeake LULC (after resampling):

- `<tag>_stack.tif` — analysis-ready multi-band GeoTIFF (all variables)
- `<tag>_{LST,NDVI,NDBI,NDWI,ALBEDO}.tif` — one file per variable
- `<tag>_CLEAR_OBS.tif` — clear-observation count (QA)
- `<tag>_manifest.geojson` — machine-readable provenance sidecar

where `<tag>` is e.g. `MD_DC_summer_2021_2023`.

### Where the outputs go in this repo

Landsat is a **third-party product**, so the downloaded GeoTIFFs belong in
[`../data/external/gee/`](../data/external/gee/) (mirrors the Chesapeake LULC rasters, which
also live under `data/external/`). Everything under `data/` is git-ignored except the folder
scaffold, so the rasters themselves are **not** committed — commit the manifest alongside your
notes if you want provenance in version control.

### Complementary covariates from the Chesapeake LULC

These Landsat variables are designed to sit next to the 1 m Chesapeake Bay Land Use/Land Cover
rasters (in `data/external/`), from which you can derive **imperviousness** and **tree canopy**.
Together they let you test whether naturally occurring affordable housing sits in hotter surface
environments *after* controlling for vegetation and built form.

> Configuration lives in the `CONFIG` object at the top of the script — study period, cloud
> thresholds, export grid, and which outputs to write are all set there.

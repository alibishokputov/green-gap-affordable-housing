"""Shiny dashboard: bivariate map of tree canopy % x LIHTC affordable units.

Crosses two variables on a 3x3 color grid at census-tract level for Maryland + DC:

    vertical axis (A)   tree canopy % of land area  (Chesapeake 1 m land cover)
    horizontal axis (B) LIHTC units                 (HUD LIHTC database)

The interesting cell is **high LIHTC / low canopy** - affordable housing in the
least-green tracts, i.e. the "green gap".

Run locally::

    uv run shiny run app/app.py --reload

Requires the analysis table; build it once with::

    uv run python -m greengap.dataset build

This module is also compiled to WebAssembly by ``scripts/build_shinylive.py`` and
served statically from GitHub Pages, so it must stay importable inside Pyodide:

* No ``greengap.config`` import - it pulls in ``dotenv``/``loguru``, and the path
  is trivial to derive here instead.
* No ``read_parquet`` - ``pyarrow`` does not exist in Pyodide. The build script
  bundles a slimmed GeoJSON next to this file, and ``GeoDataFrame.from_features``
  reads it with shapely alone (no pyarrow, no GDAL).
* Anything imported at module scope must be listed in the build script's
  requirements - shinylive resolves no transitive dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path

import folium
import geopandas as gpd
import numpy as np
import pandas as pd
from scipy import stats
from shiny import App, reactive, render, ui

# NOTHING here may import matplotlib, directly or transitively. Importing it
# under Pyodide starts a font-cache build that never completes (measured >330 s
# with no first paint), so the published dashboard would hang on a blank page.
# That rules out `mapclassify`, which imports matplotlib at module scope - hence
# greengap.classify, a numpy-only reimplementation pinned to mapclassify's
# results by tests/test_classify.py. Every chart here is HTML/CSS or inline SVG.

try:  # local dev: the real package is importable
    from greengap.bivariate import BIVARIATE_PALETTES
    from greengap.classify import bins as class_bins
except ModuleNotFoundError:  # shinylive: build script vendors these alongside
    from bivariate import BIVARIATE_PALETTES
    from classify import bins as class_bins

HERE = Path(__file__).resolve().parent
BUNDLED_GEOJSON = HERE / "tracts.geojson"
LOCAL_PARQUET = HERE.parent / "data" / "processed" / "tract_canopy_lihtc.parquet"

CANOPY_VARS = {
    "canopy_pct": "Tree canopy % (all, incl. over impervious)",
    "natural_canopy_pct": "Tree canopy % (natural only)",
}
LIHTC_VARS = {
    "lihtc_units_low_income": "LIHTC low-income units",
    "lihtc_units_total": "LIHTC total units",
    "lihtc_projects": "LIHTC project count",
}
# Natural breaks is the default deliberately: the LIHTC variables are strongly
# zero-inflated (~2/3 of tracts have no LIHTC at all), and quantile breaks on
# that distribution put both cut points at 0, collapsing the LIHTC axis to two
# classes and emptying the high-LIHTC corner - the one the study is about.
SCHEMES = {
    "natural_breaks": "Natural breaks (Jenks)",
    "equal_interval": "Equal interval",
    "quantiles": "Quantiles (equal count)",
}


def load_data() -> tuple[gpd.GeoDataFrame, int]:
    """Load the analysis table.

    Prefers the GeoJSON the build script bundles (the only option under Pyodide,
    which has no pyarrow); falls back to the parquet for local development.
    """
    if BUNDLED_GEOJSON.exists():
        raw = json.loads(BUNDLED_GEOJSON.read_text())
        gdf = gpd.GeoDataFrame.from_features(raw["features"], crs="EPSG:4326")
        # The build script already dropped unmeasurable tracts and simplified.
        return gdf, int(raw.get("unmeasured_tracts", 0))

    if not LOCAL_PARQUET.exists():
        raise FileNotFoundError(
            f"No data found. Expected the bundled {BUNDLED_GEOJSON.name} or "
            f"{LOCAL_PARQUET}.\nBuild it first:  uv run python -m greengap.dataset build"
        )
    gdf = gpd.read_parquet(LOCAL_PARQUET)

    # Drop tracts whose canopy could not be measured (open-water tracts, and the
    # military bases the Chesapeake land cover leaves as nodata). Their canopy is
    # NaN, and NaN would otherwise be binned into the *low canopy* class - i.e.
    # open Bay water would render as if it were a treeless neighbourhood.
    n_dropped = int((~gdf["canopy_reliable"]).sum())
    gdf = gdf[gdf["canopy_reliable"]].copy()

    # Simplify purely for browser rendering speed; stats use the real geometry.
    gdf["geometry"] = gdf.geometry.simplify(0.0003, preserve_topology=True)
    return gdf, n_dropped


GDF, N_UNMEASURED = load_data()
COUNTIES = sorted(GDF["county"].dropna().unique().tolist())
DEFAULT_COUNTIES = [c for c in ("Baltimore city", "District of Columbia") if c in COUNTIES]


# --------------------------------------------------------------------------- #
# UI                                                                           #
# --------------------------------------------------------------------------- #
app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_selectize(
            "counties",
            "Counties / jurisdictions",
            choices=COUNTIES,
            selected=DEFAULT_COUNTIES,
            multiple=True,
        ),
        ui.input_action_button("all_counties", "Select all counties", class_="btn-sm"),
        ui.hr(),
        ui.input_select("canopy_var", "Canopy measure", choices=CANOPY_VARS),
        ui.input_select("lihtc_var", "LIHTC measure", choices=LIHTC_VARS),
        ui.hr(),
        ui.input_select("scheme", "Classification", choices=SCHEMES),
        ui.input_select(
            "palette", "Palette", choices={k: k for k in BIVARIATE_PALETTES}
        ),
        ui.input_radio_buttons(
            "classify_on",
            "Classify breaks on",
            choices={"selection": "Visible selection", "state": "Entire state"},
            selected="selection",
        ),
        ui.input_switch("lihtc_only", "Only tracts with LIHTC units", value=False),
        ui.hr(),
        ui.output_ui("legend"),
        ui.p(
            f"{N_UNMEASURED} tract(s) excluded: canopy not measurable "
            "(open-water tracts; military land is nodata in the Chesapeake raster).",
            class_="text-muted small",
        ),
        width=330,
    ),
    ui.layout_columns(
        ui.value_box("Tracts shown", ui.output_text("n_tracts")),
        ui.value_box("Tracts with LIHTC", ui.output_text("n_lihtc")),
        ui.value_box("LIHTC units", ui.output_text("n_units")),
        ui.value_box("Canopy x LIHTC (Spearman)", ui.output_text("corr")),
        fill=False,
    ),
    ui.output_ui("note"),
    ui.navset_card_tab(
        ui.nav_panel("Bivariate map", ui.output_ui("map")),
        ui.nav_panel("Joint distribution", ui.output_ui("heatmap")),
        ui.nav_panel("Scatter", ui.output_ui("scatter")),
        ui.nav_panel(
            "Green-gap tracts",
            ui.p(
                "Tracts in the high-LIHTC / low-canopy corner of the grid - "
                "the most affordable housing in the least green places.",
                class_="text-muted small",
            ),
            ui.output_data_frame("gap_table"),
        ),
    ),
    title="Green Gap - tree canopy x LIHTC affordable housing (Maryland + DC)",
    fillable=True,
)


# --------------------------------------------------------------------------- #
# Server                                                                       #
# --------------------------------------------------------------------------- #
def server(input, output, session):
    @reactive.effect
    @reactive.event(input.all_counties)
    def _select_all():
        ui.update_selectize("counties", selected=COUNTIES)

    @reactive.calc
    def base() -> gpd.GeoDataFrame:
        """Rows eligible for classification (before the visible-county filter)."""
        gdf = GDF
        if input.lihtc_only():
            gdf = gdf[gdf[input.lihtc_var()] > 0]
        return gdf

    @reactive.calc
    def selected() -> gpd.GeoDataFrame:
        gdf = base()
        counties = list(input.counties())
        if counties:
            gdf = gdf[gdf["county"].isin(counties)]
        return gdf

    @reactive.calc
    def classed() -> gpd.GeoDataFrame:
        """Attach bivariate classes + colors to the visible selection.

        ``classify_on`` decides whether the 3x3 breaks are computed from the
        visible selection (classes adapt to what you're looking at) or from the
        whole state (classes stay comparable across county filters).

        Breaks come from ``mapclassify`` on the source rows and are then applied
        to the visible rows via ``digitize``, so both modes share one code path.
        Note the LIHTC variables are zero-inflated (most tracts have no LIHTC at
        all), which makes quantile breaks degenerate - see ``class_note``.
        """
        a, b = input.canopy_var(), input.lihtc_var()
        vis = selected()
        if vis.empty:
            return vis

        source = base() if input.classify_on() == "state" else vis
        out = vis.copy()
        colors = BIVARIATE_PALETTES[input.palette()]

        for col, cls in ((a, "_bi_a"), (b, "_bi_b")):
            ref = source[col].dropna().to_numpy(dtype="float64")
            values = out[col].to_numpy(dtype="float64")
            if len(ref) < 3 or np.unique(ref).size < 3:
                k = np.zeros(len(out), dtype=int)  # too few distinct values
            else:
                # Breaks come from `source` but are applied to the visible rows,
                # so both classify modes share one code path.
                edges = class_bins(ref, input.scheme(), k=3)
                k = np.clip(np.digitize(values, edges[:-1], right=True), 0, 2)
            k[np.isnan(values)] = 0
            out[cls] = k

        idx = out["_bi_a"].to_numpy() * 3 + out["_bi_b"].to_numpy()
        out["bi_color"] = [colors[i] for i in idx]
        return out

    @reactive.calc
    def class_note() -> str:
        """Warn when zero-inflation has collapsed the LIHTC axis to 2 classes."""
        gdf = classed()
        if gdf.empty:
            return ""
        used = sorted(gdf["_bi_b"].unique())
        if len(used) < 3:
            zeros = int((selected()[input.lihtc_var()] == 0).sum())
            pct = zeros / max(len(selected()), 1) * 100
            return (
                f"Heads-up: {zeros:,} of {len(selected()):,} tracts ({pct:.0f}%) have zero "
                f"{LIHTC_VARS[input.lihtc_var()]}, so the {input.scheme()} breaks collapse "
                "the LIHTC axis to fewer than 3 distinct classes. Turn on "
                "'Only tracts with LIHTC units' to classify within the tracts that "
                "actually have affordable housing."
            )
        return ""

    # ---- value boxes ----
    @render.text
    def n_tracts():
        return f"{len(selected()):,}"

    @render.text
    def n_lihtc():
        gdf = selected()
        return f"{int((gdf[input.lihtc_var()] > 0).sum()):,}"

    @render.text
    def n_units():
        gdf = selected()
        return f"{int(gdf[input.lihtc_var()].sum()):,}"

    @render.text
    def corr():
        gdf = selected()
        sub = gdf[[input.canopy_var(), input.lihtc_var()]].dropna()
        if len(sub) < 3:
            return "n/a"
        rho, p = stats.spearmanr(sub.iloc[:, 0], sub.iloc[:, 1])
        star = "*" if p < 0.05 else ""
        return f"{rho:+.2f}{star}"

    @render.ui
    def note():
        msg = class_note()
        if not msg:
            return None
        return ui.div(msg, class_="alert alert-warning py-2 small mb-2")

    # ---- map ----
    @render.ui
    def map():
        gdf = classed()
        if gdf.empty:
            return ui.div("No tracts match the current filters.", class_="p-4 text-muted")

        # Centre from the bounding box, NOT gdf.geometry.union_all().centroid:
        # unioning 1600+ simplified polygons is expensive, and GEOS throws a
        # TopologyException on the self-intersections that simplification leaves
        # behind - which hard-crashes the WebAssembly runtime in the static build.
        # fit_bounds below sets the real view anyway, so the union bought nothing.
        minx, miny, maxx, maxy = gdf.total_bounds
        m = folium.Map(
            location=[(miny + maxy) / 2, (minx + maxx) / 2],
            zoom_start=9,
            tiles="CartoDB positron",
        )

        a, b = input.canopy_var(), input.lihtc_var()
        cols = ["GEOID", "county", "NAMELSAD", a, b, "bi_color"]
        show = gdf[cols + ["geometry"]].copy()
        show[a] = show[a].round(1)

        folium.GeoJson(
            show.to_json(),
            style_function=lambda f: {
                "fillColor": f["properties"]["bi_color"],
                "color": "#555555",
                "weight": 0.25,
                "fillOpacity": 0.85,
            },
            highlight_function=lambda f: {"weight": 2, "color": "#000000"},
            tooltip=folium.GeoJsonTooltip(
                fields=["NAMELSAD", "county", a, b],
                aliases=["Tract", "County", CANOPY_VARS[a], LIHTC_VARS[b]],
                sticky=True,
            ),
        ).add_to(m)

        m.fit_bounds([[gdf.total_bounds[1], gdf.total_bounds[0]],
                      [gdf.total_bounds[3], gdf.total_bounds[2]]])

        # folium's default _repr_html_ iframe collapses inside Shiny's fillable
        # layout; pin the root element so the map actually has a height.
        m.get_root().width = "100%"
        m.get_root().height = "620px"
        return ui.HTML(m.get_root()._repr_html_())

    def _cell_counts() -> np.ndarray:
        """Tract count in each of the 9 bivariate classes (row = canopy)."""
        gdf = classed()
        grid = np.zeros((3, 3), dtype=int)
        if gdf.empty:
            return grid
        for row in range(3):
            for col in range(3):
                grid[row, col] = int(((gdf["_bi_a"] == row) & (gdf["_bi_b"] == col)).sum())
        return grid

    def _grid_html(*, cell_size: str, font: str, show_axis_labels: bool) -> ui.Tag:
        """The 3x3 colour grid as HTML - deliberately not matplotlib.

        Rendering this as a PNG would drag in pyplot, whose font-cache build
        stalls the WebAssembly build for minutes on first paint.
        """
        colors = BIVARIATE_PALETTES[input.palette()]
        grid = _cell_counts()

        rows = []
        for row in (2, 1, 0):  # top row = high canopy
            cells = []
            for col in range(3):
                is_gap = row == 0 and col == 2  # low canopy + high LIHTC
                cells.append(
                    ui.div(
                        str(grid[row, col]),
                        style=(
                            f"background:{colors[row * 3 + col]};"
                            f"width:{cell_size};height:{cell_size};"
                            f"display:flex;align-items:center;justify-content:center;"
                            f"font-size:{font};font-weight:600;"
                            f"color:{'#fff' if row + col >= 3 else '#111'};"
                            + ("outline:2.5px solid #d7191c;outline-offset:-2px;" if is_gap else "")
                        ),
                    )
                )
            rows.append(ui.div(*cells, style="display:flex;"))

        parts = [ui.div(*rows, style="display:inline-block;border:1px solid #ccc;")]
        if show_axis_labels:
            parts.append(
                ui.p(
                    f"→ {LIHTC_VARS[input.lihtc_var()]}",
                    class_="text-muted small mb-0 mt-1",
                )
            )
            parts.insert(
                0,
                ui.p(f"↑ {CANOPY_VARS[input.canopy_var()]}", class_="text-muted small mb-1"),
            )
        return ui.div(*parts)

    # ---- legend (3x3 swatch annotated with tract counts) ----
    @render.ui
    def legend():
        return ui.div(
            _grid_html(cell_size="52px", font="0.8rem", show_axis_labels=True),
            ui.p(
                "Red outline = the green gap: low canopy, high LIHTC.",
                class_="text-muted small mt-1 mb-0",
            ),
        )

    # ---- joint distribution ----
    @render.ui
    def heatmap():
        grid = _cell_counts()
        total = int(grid.sum())
        return ui.div(
            ui.h5("Tract counts by bivariate class"),
            _grid_html(cell_size="110px", font="1.3rem", show_axis_labels=True),
            ui.p(
                f"{total:,} tracts classified. The red-outlined cell is the "
                "green gap: the least-green tracts carrying the most affordable housing.",
                class_="text-muted small mt-2",
            ),
            class_="p-2",
        )

    # ---- scatter (inline SVG, deliberately not matplotlib) ----
    @render.ui
    def scatter():
        """Canopy vs LIHTC scatter with an OLS fit, emitted as raw SVG.

        matplotlib is avoided everywhere in this app: importing pyplot under
        Pyodide triggers a font-cache build that never completes (measured >330 s
        with no first paint), which would make the published dashboard a white
        page. Hand-rolled SVG has no dependency, renders instantly, and stays
        crisp at any zoom.
        """
        gdf = selected()
        a, b = input.canopy_var(), input.lihtc_var()
        sub = gdf[[a, b]].dropna()
        if sub.empty:
            return ui.div("No data for the current filters.", class_="p-4 text-muted")

        w, h = 720, 460
        pad_l, pad_b, pad_t, pad_r = 62, 52, 16, 16
        plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b

        x = sub[b].to_numpy(dtype="float64")
        y = sub[a].to_numpy(dtype="float64")
        x_max = max(float(x.max()), 1.0)
        y_max = max(float(y.max()), 1.0)

        def px(v: float) -> float:
            return pad_l + (v / x_max) * plot_w

        def py(v: float) -> float:
            return pad_t + plot_h - (v / y_max) * plot_h

        parts = [
            f'<rect x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" '
            f'fill="#fbfbfb" stroke="#ddd"/>'
        ]

        # gridlines + ticks
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            gy = pad_t + plot_h * (1 - frac)
            parts.append(
                f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{pad_l + plot_w}" y2="{gy:.1f}" '
                f'stroke="#eee"/>'
                f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" font-size="11" fill="#666" '
                f'text-anchor="end">{y_max * frac:.0f}</text>'
            )
            gx = pad_l + plot_w * frac
            parts.append(
                f'<line x1="{gx:.1f}" y1="{pad_t}" x2="{gx:.1f}" y2="{pad_t + plot_h}" '
                f'stroke="#eee"/>'
                f'<text x="{gx:.1f}" y="{pad_t + plot_h + 16}" font-size="11" fill="#666" '
                f'text-anchor="middle">{x_max * frac:.0f}</text>'
            )

        for xi, yi in zip(x, y):
            parts.append(
                f'<circle cx="{px(xi):.1f}" cy="{py(yi):.1f}" r="3" '
                f'fill="#2a5a5b" fill-opacity="0.5"/>'
            )

        caption = ""
        if len(sub) > 2 and sub[b].nunique() > 1:
            slope, intercept = np.polyfit(x, y, 1)
            x0, x1 = 0.0, x_max
            parts.append(
                f'<line x1="{px(x0):.1f}" y1="{py(intercept):.1f}" '
                f'x2="{px(x1):.1f}" y2="{py(slope * x1 + intercept):.1f}" '
                f'stroke="#c85a5a" stroke-width="2"/>'
            )
            caption = (
                f"OLS slope {slope:+.4f} canopy-points per unit "
                f"(intercept {intercept:.1f}%)"
            )

        parts.append(
            f'<text x="{pad_l + plot_w / 2}" y="{h - 6}" font-size="12" fill="#333" '
            f'text-anchor="middle">{LIHTC_VARS[b]}</text>'
        )
        parts.append(
            f'<text x="14" y="{pad_t + plot_h / 2}" font-size="12" fill="#333" '
            f'text-anchor="middle" transform="rotate(-90 14 {pad_t + plot_h / 2})">'
            f"{CANOPY_VARS[a]}</text>"
        )

        svg = (
            f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="Scatter of {CANOPY_VARS[a]} against {LIHTC_VARS[b]}">'
            + "".join(parts)
            + "</svg>"
        )
        return ui.div(
            ui.HTML(svg),
            ui.p(caption, class_="text-muted small mt-1"),
            class_="p-2",
        )

    # ---- green-gap table ----
    @render.data_frame
    def gap_table():
        gdf = classed()
        if gdf.empty:
            return pd.DataFrame()
        a, b = input.canopy_var(), input.lihtc_var()
        gap = gdf[(gdf["_bi_a"] == 0) & (gdf["_bi_b"] == 2)]
        out = (
            gap[["GEOID", "NAMELSAD", "county", a, b]]
            .sort_values(b, ascending=False)
            .rename(columns={a: CANOPY_VARS[a], b: LIHTC_VARS[b], "NAMELSAD": "Tract"})
        )
        out[CANOPY_VARS[a]] = out[CANOPY_VARS[a]].round(1)
        return render.DataGrid(out, height="420px")


app = App(app_ui, server)

"""Shiny dashboard: multifamily housing type x parcel-scale environment (MD + DC).

Where the bivariate dashboard (``app.py``) shows LIHTC-vs-environment co-location
across census areal units, this one works at the **building** scale and adds the
comparison the areal view cannot make: subsidised vs NOAH vs market-rate multifamily,
contrasted on their parcel-scale canopy and summer heat.

Tabs:

    Type map        block-group choropleth of an environmental measure, with
                    buildings as points coloured by housing type (legend included)
    Type choropleth block-group units by housing type (NOAH / market / subsidised)
    Type contrast   median environmental exposure per type, per state
    Value x env     value/unit against an environmental measure, NOAH cutoff drawn
    Adjusted gaps   type-vs-market regression gaps, unadjusted and controls-adjusted

Housing type is descriptive: subsidised = within 30 m of an active LIHTC/NHPD
record; NOAH = unsubsidised below the per-state value/unit cutoff; market-rate =
unsubsidised above it. The unadjusted contrasts are raw differences in medians; the
adjusted gaps come from a per-state regression on housing type + structure and
neighbourhood controls with jurisdiction fixed effects (precomputed - statsmodels
is unavailable under Pyodide). All of it is associational, not causal.

Run locally::

    uv run shiny run app/housing_app.py --reload

Reads ``buildings_types.geojson``, ``bg_types.geojson``, ``type_regression.json``
exported by ``greengap.type_regression_data export``. Pyodide-safe: no matplotlib,
no pyarrow; GeoJSON is read with ``GeoDataFrame.from_features``.
"""

from __future__ import annotations

import json
from pathlib import Path

import folium
import geopandas as gpd
import numpy as np
from shiny import App, reactive, render, ui

HERE = Path(__file__).resolve().parent
POINTS_GEOJSON = HERE / "buildings_types.geojson"
BG_GEOJSON = HERE / "bg_types.geojson"
REG_JSON = HERE / "type_regression.json"

# Housing types, display order + a colour-blind-safe palette (Okabe-Ito subset).
TYPE_ORDER = ["subsidised", "noah", "market_rate"]
TYPE_LABELS = {
    "subsidised": "Subsidised (LIHTC / NHPD)",
    "noah": "NOAH (unsubsidised, below cutoff)",
    "market_rate": "Market-rate",
    "unknown": "Unknown (no unit count)",
}
TYPE_COLORS = {
    "subsidised": "#0072B2",   # blue
    "noah": "#009E73",         # green
    "market_rate": "#D55E00",  # vermillion
    "unknown": "#999999",
}

# Environmental measures; `worse` records the disadvantageous direction, `unit` the
# reading of a gap so labels stay honest ("+4.5 pp of canopy", "-0.4 degC").
ENV_VARS = {
    "canopy_pct": {"label": "Tree canopy % (all)", "worse": "low", "unit": "pp"},
    "natural_canopy_pct": {"label": "Tree canopy % (natural)", "worse": "low", "unit": "pp"},
    "mean_lst": {"label": "Summer surface temp (°C)", "worse": "high", "unit": "°C"},
    "mean_ndvi": {"label": "NDVI (vegetation)", "worse": "low", "unit": ""},
}
ENV_LABELS = {k: v["label"] for k, v in ENV_VARS.items()}

# Sequential ramps for the choropleths (light -> dark). Canopy/NDVI: greener = more;
# LST: hotter = redder. Chosen 5-step ramps, colour-blind-reasonable.
RAMP_GREEN = ["#f7fcf5", "#c7e9c0", "#74c476", "#31a354", "#006d2c"]
RAMP_HEAT = ["#fee5d9", "#fcae91", "#fb6a4a", "#de2d26", "#a50f15"]
RAMP_BLUE = ["#f7fbff", "#c6dbef", "#6baed6", "#3182bd", "#08519c"]


def _load_points() -> gpd.GeoDataFrame:
    with open(POINTS_GEOJSON) as f:
        gj = json.load(f)
    gdf = gpd.GeoDataFrame.from_features(gj["features"], crs="EPSG:4326")
    gdf["housing_type"] = gdf["housing_type"].astype("string")
    for c in ("units", "value_per_unit", "canopy_pct", "natural_canopy_pct",
              "mean_lst", "mean_ndvi", "year_built"):
        gdf[c] = gdf[c].astype("float64")
    return gdf


def _load_bg() -> gpd.GeoDataFrame:
    with open(BG_GEOJSON) as f:
        gj = json.load(f)
    return gpd.GeoDataFrame.from_features(gj["features"], crs="EPSG:4326")


def _load_reg() -> dict:
    with open(REG_JSON) as f:
        return json.load(f)


POINTS = _load_points()
BG = _load_bg()
REG = _load_reg()
STATES = {"All": "All jurisdictions", "MD": "Maryland (6 counties)", "DC": "District of Columbia"}


def _ramp_for(env_var: str) -> list[str]:
    if env_var == "mean_lst":
        return RAMP_HEAT
    if env_var == "mean_ndvi":
        return RAMP_BLUE
    return RAMP_GREEN


def _quantile_bins(values: np.ndarray, ramp: list[str]) -> tuple[np.ndarray, list[str]]:
    v = values[np.isfinite(values)]
    if v.size < len(ramp):
        return np.array([]), ramp
    edges = np.quantile(v, np.linspace(0, 1, len(ramp) + 1))
    edges = np.unique(edges)
    return edges, ramp[: max(len(edges) - 1, 1)]


def _type_legend_html() -> str:
    items = "".join(
        f'<div style="display:flex;align-items:center;margin:2px 0">'
        f'<span style="width:12px;height:12px;border-radius:50%;'
        f'background:{TYPE_COLORS[t]};display:inline-block;margin-right:6px"></span>'
        f'<span style="font-size:12px">{TYPE_LABELS[t]}</span></div>'
        for t in TYPE_ORDER
    )
    return f'<div style="padding:6px 2px"><b style="font-size:12px">Housing type</b>{items}</div>'


# --------------------------------------------------------------------------- #
# UI                                                                           #
# --------------------------------------------------------------------------- #
app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_select("state", "State", choices=STATES, selected="All"),
        ui.input_selectize(
            "types", "Housing types shown",
            choices={t: TYPE_LABELS[t] for t in TYPE_ORDER},
            selected=TYPE_ORDER, multiple=True,
        ),
        ui.input_select("env_var", "Environmental measure", choices=ENV_LABELS,
                        selected="canopy_pct"),
        ui.hr(),
        ui.output_ui("legend"),
        ui.hr(),
        ui.markdown(
            "**NOAH** is unsubsidised multifamily below the per-state assessed "
            "value/unit cutoff. **Market-rate** is unsubsidised above it. Types "
            "differ in siting and age; gaps are descriptive, not causal."
        ),
        width=340,
    ),
    ui.layout_columns(
        ui.value_box("Buildings shown", ui.output_text("n_buildings")),
        ui.value_box("NOAH units", ui.output_text("noah_units")),
        ui.value_box("Market-rate units", ui.output_text("mkt_units")),
        ui.value_box(ui.output_text("gap_label"), ui.output_text("gap_value")),
        fill=False,
    ),
    ui.navset_card_tab(
        ui.nav_panel("Type map", ui.output_ui("map")),
        ui.nav_panel("Type choropleth", ui.output_ui("choropleth")),
        ui.nav_panel("Type contrast", ui.output_ui("contrast")),
        ui.nav_panel("Value × environment", ui.output_ui("scatter")),
        ui.nav_panel("Adjusted gaps", ui.output_ui("regression")),
    ),
    title="Housing type × environment — multifamily rental (MD + DC)",
    fillable=True,
)


# --------------------------------------------------------------------------- #
# Server                                                                       #
# --------------------------------------------------------------------------- #
def server(input, output, session):
    @reactive.calc
    def view() -> gpd.GeoDataFrame:
        gdf = POINTS
        if input.state() != "All":
            gdf = gdf[gdf["state"] == input.state()]
        types = list(input.types()) or TYPE_ORDER
        return gdf[gdf["housing_type"].isin(types)].copy()

    @render.ui
    def legend():
        return ui.HTML(_type_legend_html())

    # ---- value boxes ----
    @render.text
    def n_buildings():
        return f"{len(view()):,}"

    @render.text
    def noah_units():
        g = view()
        return f"{int(g.loc[g['housing_type'] == 'noah', 'units'].fillna(0).sum()):,}"

    @render.text
    def mkt_units():
        g = view()
        return f"{int(g.loc[g['housing_type'] == 'market_rate', 'units'].fillna(0).sum()):,}"

    @render.text
    def gap_label():
        u = ENV_VARS[input.env_var()]["unit"]
        u = f" ({u})" if u else ""
        return f"NOAH − market, median{u}"

    @render.text
    def gap_value():
        # Explicitly a difference in medians, not a correlation.
        g = view()
        v = input.env_var()
        noah = g.loc[g["housing_type"] == "noah", v].median()
        mkt = g.loc[g["housing_type"] == "market_rate", v].median()
        if np.isnan(noah) or np.isnan(mkt):
            return "—"
        return f"{noah - mkt:+.1f}"

    # ---- Tab 1: environmental choropleth + typed points ----
    @render.ui
    def map():
        g = view()
        if g.empty:
            return ui.div("No buildings match the filters.", class_="p-4 text-muted")
        v = input.env_var()

        bg = BG if input.state() == "All" else BG[BG["GEOID"].str.startswith(
            "24" if input.state() == "MD" else "11")]
        minx, miny, maxx, maxy = g.total_bounds
        m = folium.Map(location=[(miny + maxy) / 2, (minx + maxx) / 2],
                       zoom_start=9, tiles="CartoDB positron")

        # Environmental choropleth backdrop (quantile-binned).
        ramp = _ramp_for(v)
        vals = bg[v].to_numpy(dtype="float64")
        edges, colors = _quantile_bins(vals, ramp)
        if edges.size:
            def color_for(x):
                if x is None or (isinstance(x, float) and np.isnan(x)):
                    return "#00000000"
                k = int(np.clip(np.digitize([x], edges[1:-1])[0], 0, len(colors) - 1))
                return colors[k]
            show_bg = bg[["GEOID", v, "geometry"]].copy()
            show_bg[v] = show_bg[v].round(1)
            folium.GeoJson(
                show_bg.to_json(),
                style_function=lambda f: {
                    "fillColor": color_for(f["properties"][v]),
                    "color": "#bbbbbb", "weight": 0.2, "fillOpacity": 0.75,
                },
                tooltip=folium.GeoJsonTooltip(fields=[v], aliases=[ENV_LABELS[v]]),
            ).add_to(m)

        # Typed building points on top (single GeoJson layer for a light payload).
        show = g[["housing_type", "jurisdiction", "units", v, "geometry"]].copy()
        show[v] = show[v].round(1)
        show["type_label"] = show["housing_type"].map(TYPE_LABELS)
        show["_color"] = show["housing_type"].map(TYPE_COLORS).fillna("#999999")
        folium.GeoJson(
            show.to_json(),
            marker=folium.CircleMarker(radius=2.5, fill=True, fill_opacity=0.85, weight=0.3),
            style_function=lambda f: {
                "color": "#333333", "weight": 0.3,
                "fillColor": f["properties"]["_color"],
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["type_label", "jurisdiction", "units", v],
                aliases=["Type", "Jurisdiction", "Units", ENV_LABELS[v]],
            ),
        ).add_to(m)

        m.fit_bounds([[miny, minx], [maxy, maxx]])
        m.get_root().width = "100%"
        m.get_root().height = "620px"
        return ui.HTML(m.get_root()._repr_html_())

    # ---- Tab 2: block-group choropleth of units by type ----
    @render.ui
    def choropleth():
        types = list(input.types()) or TYPE_ORDER
        cols = [f"units_{t}" for t in types if f"units_{t}" in BG.columns]
        if not cols:
            return ui.div("No unit columns for the selected types.", class_="p-4 text-muted")
        bg = BG if input.state() == "All" else BG[BG["GEOID"].str.startswith(
            "24" if input.state() == "MD" else "11")].copy()
        bg = bg.copy()
        bg["_units"] = bg[cols].sum(axis=1)

        minx, miny, maxx, maxy = bg.total_bounds
        m = folium.Map(location=[(miny + maxy) / 2, (minx + maxx) / 2],
                       zoom_start=9, tiles="CartoDB positron")
        # Purple sequential ramp for "units of the selected type(s)".
        ramp = ["#fcfbfd", "#dadaeb", "#9e9ac8", "#756bb1", "#54278f"]
        vals = bg.loc[bg["_units"] > 0, "_units"].to_numpy(dtype="float64")
        edges, colors = _quantile_bins(vals, ramp)

        def color_for(x):
            if not x or x <= 0:
                return "#00000000"
            if not edges.size:
                return colors[-1]
            k = int(np.clip(np.digitize([x], edges[1:-1])[0], 0, len(colors) - 1))
            return colors[k]

        show = bg[["GEOID", "county", "_units", "geometry"]].copy()
        label = " + ".join(TYPE_LABELS[t].split(" (")[0] for t in types)
        folium.GeoJson(
            show.to_json(),
            style_function=lambda f: {
                "fillColor": color_for(f["properties"]["_units"]),
                "color": "#cccccc", "weight": 0.2, "fillOpacity": 0.8,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["county", "_units"], aliases=["County", f"Units ({label})"]),
        ).add_to(m)
        m.fit_bounds([[miny, minx], [maxy, maxx]])
        m.get_root().width = "100%"
        m.get_root().height = "620px"
        return ui.HTML(
            f'<div style="font-size:13px;margin:6px 2px;color:#444">Block-group units of: '
            f'<b>{label}</b> (darker = more units)</div>' + m.get_root()._repr_html_())

    # ---- Tab 3: type contrast (median per type, per state) ----
    @render.ui
    def contrast():
        g = view()
        v = input.env_var()
        rows = []
        for state in ("MD", "DC"):
            gs = g[g["state"] == state]
            for t in TYPE_ORDER:
                vals = gs.loc[gs["housing_type"] == t, v].dropna()
                if len(vals):
                    rows.append((state, t, float(vals.median()), len(vals)))
        if not rows:
            return ui.div("No data for the current selection.", class_="p-4 text-muted")
        vmax = max(r[2] for r in rows) or 1.0
        bar_w = 360
        html = ['<div style="padding:12px 8px;font-family:system-ui,sans-serif">']
        html.append(f'<div style="font-weight:600;margin-bottom:8px">'
                    f'Median {ENV_LABELS[v]} by housing type (unadjusted)</div>')
        for state in ("MD", "DC"):
            sr = [r for r in rows if r[0] == state]
            if not sr:
                continue
            html.append(f'<div style="margin:10px 0 4px;font-weight:600;color:#444">'
                        f'{STATES.get(state, state)}</div>')
            for _, t, med, n in sr:
                w = int(bar_w * med / vmax)
                html.append(
                    '<div style="display:flex;align-items:center;margin:3px 0">'
                    f'<div style="width:210px;font-size:13px">{TYPE_LABELS[t]}</div>'
                    f'<div style="width:{w}px;height:16px;background:{TYPE_COLORS[t]};'
                    'border-radius:2px"></div>'
                    f'<div style="margin-left:8px;font-size:13px">{med:.1f} '
                    f'<span style="color:#888">(n={n})</span></div></div>'
                )
        html.append('<div style="margin-top:12px;font-size:12px;color:#888">'
                    'Raw medians, no controls. Per state deliberately: pooling MD and '
                    'DC hides that the type–environment relationship differs between '
                    'them. See the Adjusted gaps tab for controls-adjusted contrasts.</div>')
        html.append("</div>")
        return ui.HTML("".join(html))

    # ---- Tab 4: value/unit x environment, NOAH cutoff drawn ----
    @render.ui
    def scatter():
        g = view()
        v = input.env_var()
        d = g.dropna(subset=["value_per_unit", v])
        d = d[d["value_per_unit"] <= 1_000_000]
        if d.empty:
            return ui.div("No buildings with both measures.", class_="p-4 text-muted")
        w, h, pad = 640, 420, 44
        xs, ys = d[v].to_numpy(dtype="float64"), d["value_per_unit"].to_numpy(dtype="float64")
        x0, x1 = float(np.nanmin(xs)), float(np.nanmax(xs))
        y0, y1 = 0.0, float(np.nanpercentile(ys, 99))
        xr, yr = (x1 - x0) or 1.0, (y1 - y0) or 1.0

        def px(x):
            return pad + (x - x0) / xr * (w - 2 * pad)

        def py(y):
            return h - pad - (min(y, y1) - y0) / yr * (h - 2 * pad)

        pts = [
            f'<circle cx="{px(r[v]):.1f}" cy="{py(r["value_per_unit"]):.1f}" r="2.2" '
            f'fill="{TYPE_COLORS.get(str(r["housing_type"]), "#999")}" fill-opacity="0.5"/>'
            for _, r in d.iterrows()
        ]
        noah_vpu = d.loc[d["housing_type"] == "noah", "value_per_unit"]
        cut = ""
        if len(noah_vpu):
            yc = py(float(noah_vpu.max()))
            cut = (f'<line x1="{pad}" y1="{yc:.1f}" x2="{w - pad}" y2="{yc:.1f}" '
                   'stroke="#333" stroke-dasharray="5,4" stroke-width="1"/>'
                   f'<text x="{w - pad}" y="{yc - 4:.1f}" text-anchor="end" font-size="11" '
                   'fill="#333">NOAH / market cutoff</text>')
        legend = "".join(
            f'<circle cx="{w - pad - 130}" cy="{pad + i * 16}" r="4" fill="{TYPE_COLORS[t]}"/>'
            f'<text x="{w - pad - 120}" y="{pad + i * 16 + 4}" font-size="11" fill="#333">'
            f'{TYPE_LABELS[t].split(" (")[0]}</text>'
            for i, t in enumerate(TYPE_ORDER)
        )
        svg = (
            f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            'font-family="system-ui,sans-serif">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#999"/>'
            f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" stroke="#999"/>'
            f'<text x="{w / 2:.0f}" y="{h - 8}" text-anchor="middle" font-size="12">'
            f'{ENV_LABELS[v]}</text>'
            f'<text x="14" y="{h / 2:.0f}" text-anchor="middle" font-size="12" '
            f'transform="rotate(-90 14 {h / 2:.0f})">Assessed value per unit ($)</text>'
            + "".join(pts) + cut + legend + "</svg>"
        )
        return ui.HTML(svg)

    # ---- Tab 5: adjusted regression gaps ----
    @render.ui
    def regression():
        v = input.env_var()
        recs = REG["results"].get(v, [])
        scope = input.state() if input.state() in ("MD", "DC") else "pooled"
        rows = [r for r in recs if r["scope"] == scope]
        if not rows:
            return ui.div("No regression for this selection.", class_="p-4 text-muted")

        unit = ENV_VARS[v]["unit"]
        usfx = f" {unit}" if unit else ""
        # x-scale across all shown estimates + CIs, symmetric around 0.
        allv = [x for r in rows for x in (r["ci_low"], r["ci_high"])]
        m = max(abs(min(allv)), abs(max(allv))) or 1.0
        w, rowh, pad = 620, 34, 150

        def px(x):
            return pad + (x + m) / (2 * m) * (w - pad - 20)

        html = ['<div style="padding:12px 8px;font-family:system-ui,sans-serif">']
        html.append(f'<div style="font-weight:600">Gap vs market-rate in {ENV_LABELS[v]} '
                    f'— {STATES.get(scope, "pooled (jurisdiction FE)")}</div>')
        html.append('<div style="font-size:12px;color:#888;margin:2px 0 10px">'
                    'Each bar is a housing type minus market-rate. Hollow = unadjusted, '
                    'solid = adjusted for structure, ACS neighbourhood, and '
                    'jurisdiction. Whiskers are 95% CIs (HC3).</div>')
        svg_h = len(rows) * rowh + 40
        parts = [f'<svg viewBox="0 0 {w} {svg_h}" width="100%" height="{svg_h}" '
                 'font-family="system-ui,sans-serif">']
        x0 = px(0)
        parts.append(f'<line x1="{x0:.1f}" y1="10" x2="{x0:.1f}" y2="{svg_h - 20}" '
                     'stroke="#999" stroke-dasharray="3,3"/>')
        for i, r in enumerate(sorted(rows, key=lambda z: (z["type"], z["adjusted"]))):
            y = 24 + i * rowh
            col = TYPE_COLORS.get(r["type"], "#333")
            solid = r["adjusted"]
            parts.append(
                f'<line x1="{px(r["ci_low"]):.1f}" y1="{y}" x2="{px(r["ci_high"]):.1f}" '
                f'y2="{y}" stroke="{col}" stroke-width="1.5"/>'
                f'<circle cx="{px(r["gap_vs_market"]):.1f}" cy="{y}" r="5" '
                f'fill="{col if solid else "white"}" stroke="{col}" stroke-width="1.5"/>'
                f'<text x="4" y="{y + 4}" font-size="11" fill="#333">'
                f'{TYPE_LABELS[r["type"]].split(" (")[0]} '
                f'({"adj" if solid else "raw"})</text>'
                f'<text x="{w - 4}" y="{y + 4}" text-anchor="end" font-size="11" '
                f'fill="#555">{r["gap_vs_market"]:+.1f}{usfx}</text>'
            )
        parts.append(f'<text x="{px(-m):.1f}" y="{svg_h - 4}" font-size="10" fill="#888">'
                     f'{-m:.1f}</text>')
        parts.append(f'<text x="{px(m):.1f}" y="{svg_h - 4}" text-anchor="end" '
                     f'font-size="10" fill="#888">{m:.1f}</text>')
        parts.append("</svg>")
        html.append("".join(parts))
        html.append('<div style="margin-top:10px;font-size:12px;color:#888">'
                    'Adjusted for: structure age, log lot area, log units, block-group '
                    'income, poverty, education, race/ethnicity, population density, '
                    'and jurisdiction fixed effects. Conditional exposure '
                    'differences, not causal effects.</div>')
        html.append("</div>")
        return ui.HTML("".join(html))


app = App(app_ui, server)

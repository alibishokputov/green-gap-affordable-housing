"""Shiny dashboard: multifamily housing type x parcel-scale environment (MD + DC).

Building-scale companion to the areal bivariate dashboard. It labels multifamily
rental buildings subsidized (LIHTC) / NOAH / market-rate and contrasts them on the
canopy and summer heat measured at each building's footprint, and it carries the
areal bivariate environment-vs-LIHTC view so both scales sit together - which
matters because they can point opposite ways (the canopy paradox, see the Paradox
tab).

Tabs:

    Type map         environmental choropleth + buildings as points colored by type
                     (LIHTC buildings drawn as a distinct marker)
    Bivariate map    areal environment x LIHTC-units grid, with high-risk (green-gap)
                     block groups outlined in red and listed in a table below
    Type contrast    median environmental exposure per housing type, per state
    Value x env      value/unit against an environmental measure, NOAH cutoff drawn
    Paradox          building-level vs block-group-level canopy, reconciled honestly

Housing type is descriptive. Subsidized = within 30 m of a HUD LIHTC record (the
trusted measure; NHPD adds coverage but is kept separate). NOAH = unsubsidized with
assessed value/unit at or below an AMI-anchored affordability cutoff (60% AMI
default); market-rate = above it. Everything shown is an unconditional association,
not a causal effect.

Run locally::

    uv run shiny run app/housing_app.py --reload

Reads ``buildings_types.geojson``, ``bg_types.geojson``, ``type_stats.json``
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
STATS_JSON = HERE / "type_stats.json"

TYPE_ORDER = ["subsidized", "noah", "market_rate"]
TYPE_LABELS = {
    "subsidized": "Subsidized (LIHTC)",
    "noah": "NOAH (unsubsidized, below cutoff)",
    "market_rate": "Market-rate",
    "unknown": "Unknown (no unit count)",
}
TYPE_COLORS = {
    "subsidized": "#0072B2", "noah": "#009E73",
    "market_rate": "#D55E00", "unknown": "#999999",
}

ENV_VARS = {
    "canopy_pct": {"label": "Tree canopy % (all)", "worse": "low", "unit": "pp"},
    "natural_canopy_pct": {"label": "Tree canopy % (natural)", "worse": "low", "unit": "pp"},
    "mean_lst": {"label": "Summer surface temp (°C)", "worse": "high", "unit": "°C"},
    "mean_ndvi": {"label": "NDVI (vegetation)", "worse": "low", "unit": ""},
}
ENV_LABELS = {k: v["label"] for k, v in ENV_VARS.items()}

RAMP_GREEN = ["#f7fcf5", "#c7e9c0", "#74c476", "#31a354", "#006d2c"]
RAMP_HEAT = ["#fee5d9", "#fcae91", "#fb6a4a", "#de2d26", "#a50f15"]
RAMP_BLUE = ["#f7fbff", "#c6dbef", "#6baed6", "#3182bd", "#08519c"]

# Bivariate 3x3 palette: rows = environment (worse -> better), cols = LIHTC (low ->
# high). The green-gap corner (worst environment + most LIHTC) is the dark red at [0,2].
BIVARIATE_9 = [
    "#e8e8e8", "#b8d6be", "#73ae80",   # env low  (worst): lihtc low, mid, high
    "#e4acac", "#ad9ea5", "#5a9178",   # env mid
    "#c85a5a", "#985356", "#574249",   # env high (best)
]


def _load_points() -> gpd.GeoDataFrame:
    with open(POINTS_GEOJSON) as f:
        gj = json.load(f)
    gdf = gpd.GeoDataFrame.from_features(gj["features"], crs="EPSG:4326")
    gdf["housing_type"] = gdf["housing_type"].astype("string")
    for c in ("units", "value_per_unit", "canopy_pct", "natural_canopy_pct",
              "mean_lst", "mean_ndvi", "year_built"):
        gdf[c] = gdf[c].astype("float64")
    for c in ("lihtc", "section8"):
        if c in gdf.columns:
            gdf[c] = gdf[c].astype("boolean").fillna(False)
    return gdf


def _load_bg() -> gpd.GeoDataFrame:
    with open(BG_GEOJSON) as f:
        gj = json.load(f)
    return gpd.GeoDataFrame.from_features(gj["features"], crs="EPSG:4326")


def _load_stats() -> dict:
    with open(STATS_JSON) as f:
        return json.load(f)


POINTS = _load_points()
BG = _load_bg()
STATS = _load_stats()
STATES = {"All": "All jurisdictions", "MD": "Maryland (6 counties)", "DC": "District of Columbia"}
# Study-area counties only: the areal BG file spans all of MD+DC, but only the seven
# study jurisdictions carry multifamily buildings. Limit the filter to those.
if "county" in BG.columns:
    _unit_cols = [c for c in BG.columns if c.startswith("units_")]
    _has_bldg = BG[_unit_cols].sum(axis=1) > 0 if _unit_cols else BG["county"].notna()
    COUNTIES = sorted(BG.loc[_has_bldg, "county"].dropna().unique().tolist())
else:
    COUNTIES = []


def _ramp_for(env_var: str) -> list[str]:
    if env_var == "mean_lst":
        return RAMP_HEAT
    if env_var == "mean_ndvi":
        return RAMP_BLUE
    return RAMP_GREEN


def _tertiles(values: np.ndarray) -> np.ndarray:
    """Return the two cut points (33rd, 67th pct) of the finite values."""
    v = values[np.isfinite(values)]
    if v.size < 3:
        return np.array([np.nan, np.nan])
    return np.quantile(v, [1 / 3, 2 / 3])


def _quantile_bins(values: np.ndarray, ramp: list[str]) -> tuple[np.ndarray, list[str]]:
    v = values[np.isfinite(values)]
    if v.size < len(ramp):
        return np.array([]), ramp
    edges = np.unique(np.quantile(v, np.linspace(0, 1, len(ramp) + 1)))
    return edges, ramp[: max(len(edges) - 1, 1)]


def _type_legend_html() -> str:
    items = "".join(
        f'<div style="display:flex;align-items:center;margin:2px 0">'
        f'<span style="width:12px;height:12px;border-radius:50%;background:{TYPE_COLORS[t]};'
        f'display:inline-block;margin-right:6px"></span>'
        f'<span style="font-size:12px">{TYPE_LABELS[t]}</span></div>'
        for t in TYPE_ORDER
    )
    items += (
        '<div style="display:flex;align-items:center;margin:4px 0 2px">'
        '<span style="width:11px;height:11px;background:#0072B2;display:inline-block;'
        'margin-right:6px;transform:rotate(45deg)"></span>'
        '<span style="font-size:12px">LIHTC building (diamond)</span></div>'
    )
    return f'<div style="padding:6px 2px"><b style="font-size:12px">Housing type</b>{items}</div>'


# --------------------------------------------------------------------------- #
# UI                                                                           #
# --------------------------------------------------------------------------- #
app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.input_select("state", "State", choices=STATES, selected="All"),
        ui.input_selectize(
            "counties", "Counties / jurisdictions",
            choices=COUNTIES, selected=COUNTIES, multiple=True,
        ),
        ui.input_action_button("clear_counties", "Unselect all", class_="btn-sm"),
        ui.input_action_button("all_counties", "Select all", class_="btn-sm"),
        ui.hr(),
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
            "**NOAH** = unsubsidized multifamily with assessed value/unit at or below "
            "the 60%-AMI affordability cutoff. **Subsidized** = within 30 m of a HUD "
            "LIHTC record. Descriptive, not causal."
        ),
        width=350,
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
        ui.nav_panel("Bivariate map (env × LIHTC)",
                     ui.output_ui("bivariate_map"),
                     ui.output_ui("greengap_caption"),
                     ui.output_data_frame("greengap_table")),
        ui.nav_panel("Type contrast", ui.output_ui("contrast")),
        ui.nav_panel("Value × environment", ui.output_ui("scatter")),
        ui.nav_panel("Canopy paradox", ui.output_ui("paradox")),
    ),
    title="Housing type × environment — multifamily rental (MD + DC)",
    fillable=True,
)


# --------------------------------------------------------------------------- #
# Server                                                                       #
# --------------------------------------------------------------------------- #
def server(input, output, session):
    @reactive.effect
    @reactive.event(input.clear_counties)
    def _clear():
        ui.update_selectize("counties", selected=[])

    @reactive.effect
    @reactive.event(input.all_counties)
    def _all():
        ui.update_selectize("counties", selected=COUNTIES)

    def _selected_counties() -> list[str]:
        return list(input.counties())

    @reactive.calc
    def view() -> gpd.GeoDataFrame:
        gdf = POINTS
        if input.state() != "All":
            gdf = gdf[gdf["state"] == input.state()]
        cts = _selected_counties()
        if cts and "jurisdiction" in gdf.columns:
            # points carry jurisdiction; map county names loosely by substring
            gdf = gdf[gdf["jurisdiction"].isin(cts) | gdf["jurisdiction"].apply(
                lambda j: any(str(j) in c or c in str(j) for c in cts))]
        types = list(input.types()) or TYPE_ORDER
        return gdf[gdf["housing_type"].isin(types)].copy()

    @reactive.calc
    def bg_view() -> gpd.GeoDataFrame:
        bg = BG
        if input.state() != "All":
            bg = bg[bg["GEOID"].str.startswith("24" if input.state() == "MD" else "11")]
        cts = _selected_counties()
        if cts and "county" in bg.columns:
            bg = bg[bg["county"].isin(cts)]
        return bg.copy()

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
        return f"NOAH − market, median{f' ({u})' if u else ''}"

    @render.text
    def gap_value():
        g, v = view(), input.env_var()
        noah = g.loc[g["housing_type"] == "noah", v].median()
        mkt = g.loc[g["housing_type"] == "market_rate", v].median()
        return "—" if (np.isnan(noah) or np.isnan(mkt)) else f"{noah - mkt:+.1f}"

    # ---- Tab 1: environmental choropleth + typed points (LIHTC = diamond) ----
    @render.ui
    def map():
        g = view()
        if g.empty:
            return ui.div("No buildings match the filters.", class_="p-4 text-muted")
        v = input.env_var()
        bg = bg_view()
        minx, miny, maxx, maxy = g.total_bounds
        m = folium.Map(location=[(miny + maxy) / 2, (minx + maxx) / 2],
                       zoom_start=9, tiles="CartoDB positron")

        edges, colors = _quantile_bins(bg[v].to_numpy(dtype="float64"), _ramp_for(v))
        if edges.size:
            def color_for(x):
                if x is None or (isinstance(x, float) and np.isnan(x)):
                    return "#00000000"
                return colors[int(np.clip(np.digitize([x], edges[1:-1])[0], 0, len(colors) - 1))]
            show_bg = bg[["GEOID", v, "geometry"]].copy()
            show_bg[v] = show_bg[v].round(1)
            folium.GeoJson(
                show_bg.to_json(),
                style_function=lambda f: {"fillColor": color_for(f["properties"][v]),
                                          "color": "#bbb", "weight": 0.2, "fillOpacity": 0.7},
                tooltip=folium.GeoJsonTooltip(fields=[v], aliases=[ENV_LABELS[v]]),
            ).add_to(m)

        # Non-LIHTC points as one GeoJson circle layer (light).
        lihtc_col = "lihtc" if "lihtc" in g.columns else None
        non = g[~g[lihtc_col]] if lihtc_col else g
        show = non[["housing_type", "jurisdiction", "units", v, "geometry"]].copy()
        show[v] = show[v].round(1)
        show["type_label"] = show["housing_type"].map(TYPE_LABELS)
        show["_c"] = show["housing_type"].map(TYPE_COLORS).fillna("#999")
        folium.GeoJson(
            show.to_json(),
            marker=folium.CircleMarker(radius=2.5, fill=True, fill_opacity=0.8, weight=0.3),
            style_function=lambda f: {"color": "#333", "weight": 0.3,
                                      "fillColor": f["properties"]["_c"]},
            tooltip=folium.GeoJsonTooltip(fields=["type_label", "jurisdiction", "units", v],
                                          aliases=["Type", "Jurisdiction", "Units", ENV_LABELS[v]]),
        ).add_to(m)

        # LIHTC buildings as distinct diamonds, drawn on top. Uses a CSS divIcon
        # (a rotated square) rather than RegularPolygonMarker: that marker needs the
        # Leaflet.dvf plugin loaded from a CDN, which the offline/CSP WASM build blocks.
        if lihtc_col:
            for _, r in g[g[lihtc_col]].iterrows():
                icon = folium.DivIcon(
                    icon_size=(11, 11), icon_anchor=(6, 6),
                    html='<div style="width:9px;height:9px;background:#0072B2;'
                    'border:1px solid #003a5c;transform:rotate(45deg)"></div>',
                )
                folium.Marker(
                    location=[r.geometry.y, r.geometry.x], icon=icon,
                    tooltip=f"LIHTC · {r['jurisdiction']} · units {r['units']:.0f}",
                ).add_to(m)

        m.fit_bounds([[miny, minx], [maxy, maxx]])
        m.get_root().width = "100%"
        m.get_root().height = "600px"
        return ui.HTML(m.get_root()._repr_html_())

    # ---- bivariate classification (env x LIHTC), shared by map + table ----
    @reactive.calc
    def bivariate() -> gpd.GeoDataFrame:
        v = input.env_var()
        bg = bg_view().dropna(subset=[v]).copy()
        if bg.empty:
            return bg
        lihtc = bg["lihtc_units_low_income"].fillna(0).to_numpy(dtype="float64")
        env = bg[v].to_numpy(dtype="float64")

        # Environment class 0=worst..2=best given the measure's polarity.
        e_cut = _tertiles(env)
        e_cls = np.clip(np.digitize(env, e_cut), 0, 2)
        if ENV_VARS[v]["worse"] == "high":  # high LST is worst -> flip so 0=worst
            e_cls = 2 - e_cls

        # LIHTC class among BGs that have any LIHTC (zero-inflated otherwise).
        pos = lihtc[lihtc > 0]
        l_cut = _tertiles(pos) if pos.size >= 3 else np.array([np.nan, np.nan])
        l_cls = np.where(lihtc <= 0, 0, np.clip(np.digitize(lihtc, l_cut), 0, 2))

        bg["_e"], bg["_l"] = e_cls.astype(int), l_cls.astype(int)
        bg["_bi"] = [BIVARIATE_9[e * 3 + li] for e, li in zip(bg["_e"], bg["_l"])]
        # Green-gap = worst environment quartile AND top LIHTC class (corner cell).
        bg["greengap"] = (bg["_e"] == 0) & (bg["_l"] == 2)
        return bg

    # ---- Tab 2: bivariate map with red high-risk outline ----
    @render.ui
    def bivariate_map():
        bg = bivariate()
        if bg.empty:
            return ui.div("No block groups match the filters.", class_="p-4 text-muted")
        v = input.env_var()
        minx, miny, maxx, maxy = bg.total_bounds
        m = folium.Map(location=[(miny + maxy) / 2, (minx + maxx) / 2],
                       zoom_start=9, tiles="CartoDB positron")
        show = bg[["GEOID", "county", v, "lihtc_units_low_income", "_bi", "greengap",
                   "geometry"]].copy()
        show[v] = show[v].round(1)
        folium.GeoJson(
            show.to_json(),
            style_function=lambda f: {
                "fillColor": f["properties"]["_bi"],
                "color": "#d7191c" if f["properties"]["greengap"] else "#999",
                "weight": 2.2 if f["properties"]["greengap"] else 0.2,
                "fillOpacity": 0.8,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["county", v, "lihtc_units_low_income", "greengap"],
                aliases=["County", ENV_LABELS[v], "LIHTC units", "Green-gap?"]),
        ).add_to(m)
        m.fit_bounds([[miny, minx], [maxy, maxx]])
        m.get_root().width = "100%"
        m.get_root().height = "560px"
        return ui.HTML(m.get_root()._repr_html_())

    @render.ui
    def greengap_caption():
        bg = bivariate()
        n = int(bg["greengap"].sum()) if not bg.empty else 0
        v = ENV_LABELS[input.env_var()]
        return ui.HTML(
            f'<div style="margin:10px 2px;font-size:13px">'
            f'<b>{n}</b> green-gap block groups (red outline): worst-tertile {v} '
            f'<b>and</b> highest-tertile LIHTC units. This is spatial co-location, '
            f'not evidence that LIHTC housing is treated worse.</div>')

    @render.data_frame
    def greengap_table():
        import pandas as pd
        bg = bivariate()
        if bg.empty or not bg["greengap"].any():
            return pd.DataFrame({"note": ["No green-gap block groups in the selection."]})
        v = input.env_var()
        t = (bg[bg["greengap"]][["GEOID", "county", v, "lihtc_units_low_income"]]
             .sort_values("lihtc_units_low_income", ascending=False).copy())
        t[v] = t[v].round(1)
        t = t.rename(columns={"GEOID": "Block group", "county": "County",
                              v: ENV_LABELS[v], "lihtc_units_low_income": "LIHTC units"})
        return render.DataGrid(t, height="260px")

    # ---- Tab 3: type contrast ----
    @render.ui
    def contrast():
        g, v = view(), input.env_var()
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
        html = ['<div style="padding:12px 8px;font-family:system-ui,sans-serif">',
                f'<div style="font-weight:600;margin-bottom:8px">'
                f'Median {ENV_LABELS[v]} by housing type (unadjusted)</div>']
        for state in ("MD", "DC"):
            sr = [r for r in rows if r[0] == state]
            if not sr:
                continue
            html.append(f'<div style="margin:10px 0 4px;font-weight:600;color:#444">'
                        f'{STATES.get(state, state)}</div>')
            for _, t, med, n in sr:
                w = int(360 * med / vmax)
                html.append(
                    '<div style="display:flex;align-items:center;margin:3px 0">'
                    f'<div style="width:210px;font-size:13px">{TYPE_LABELS[t]}</div>'
                    f'<div style="width:{w}px;height:16px;background:{TYPE_COLORS[t]};'
                    'border-radius:2px"></div>'
                    f'<div style="margin-left:8px;font-size:13px">{med:.1f} '
                    f'<span style="color:#888">(n={n})</span></div></div>')
        html.append('<div style="margin-top:12px;font-size:12px;color:#888">'
                    'Raw medians, no controls. Per state deliberately: the type–'
                    'environment relationship differs between MD and DC.</div></div>')
        return ui.HTML("".join(html))

    # ---- Tab 4: value/unit x environment ----
    @render.ui
    def scatter():
        g, v = view(), input.env_var()
        d = g.dropna(subset=["value_per_unit", v])
        d = d[d["value_per_unit"] <= 1_000_000]
        if d.empty:
            return ui.div("No buildings with both measures.", class_="p-4 text-muted")
        w, h, pad = 640, 420, 44
        xs, ys = d[v].to_numpy("float64"), d["value_per_unit"].to_numpy("float64")
        x0, x1 = float(np.nanmin(xs)), float(np.nanmax(xs))
        y0, y1 = 0.0, float(np.nanpercentile(ys, 99))
        xr, yr = (x1 - x0) or 1.0, (y1 - y0) or 1.0
        px = lambda x: pad + (x - x0) / xr * (w - 2 * pad)  # noqa: E731
        py = lambda y: h - pad - (min(y, y1) - y0) / yr * (h - 2 * pad)  # noqa: E731
        pts = [f'<circle cx="{px(r[v]):.1f}" cy="{py(r["value_per_unit"]):.1f}" r="2.2" '
               f'fill="{TYPE_COLORS.get(str(r["housing_type"]), "#999")}" fill-opacity="0.5"/>'
               for _, r in d.iterrows()]
        noah_vpu = d.loc[d["housing_type"] == "noah", "value_per_unit"]
        cut = ""
        if len(noah_vpu):
            yc = py(float(noah_vpu.max()))
            cut = (f'<line x1="{pad}" y1="{yc:.1f}" x2="{w - pad}" y2="{yc:.1f}" stroke="#333" '
                   'stroke-dasharray="5,4"/><text x="{}" y="{:.1f}" text-anchor="end" '
                   'font-size="11" fill="#333">NOAH / market cutoff</text>'.format(w - pad, yc - 4))
        legend = "".join(
            f'<circle cx="{w - pad - 130}" cy="{pad + i * 16}" r="4" fill="{TYPE_COLORS[t]}"/>'
            f'<text x="{w - pad - 120}" y="{pad + i * 16 + 4}" font-size="11">'
            f'{TYPE_LABELS[t].split(" (")[0]}</text>' for i, t in enumerate(TYPE_ORDER))
        return ui.HTML(
            f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" font-family="system-ui">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#999"/>'
            f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" stroke="#999"/>'
            f'<text x="{w / 2:.0f}" y="{h - 8}" text-anchor="middle" font-size="12">'
            f'{ENV_LABELS[v]}</text>'
            f'<text x="14" y="{h / 2:.0f}" text-anchor="middle" font-size="12" '
            f'transform="rotate(-90 14 {h / 2:.0f})">Assessed value per unit ($)</text>'
            + "".join(pts) + cut + legend + "</svg>")

    # ---- Tab 5: canopy paradox, reconciled ----
    @render.ui
    def paradox():
        p = STATS["paradox"]
        corr = STATS["bg_corr"]["canopy_pct"]
        rv = STATS.get("rent_validation", {})
        bm = STATS["building_medians"]["canopy_pct"]
        html = ['<div style="padding:14px 10px;font-family:system-ui;max-width:760px">']
        html.append('<h4 style="margin:0 0 6px">Why the two scales disagree — and both are right</h4>')
        html.append(
            '<p style="font-size:13px;color:#333">At the <b>building</b> scale, affordable '
            f'buildings are greener than market-rate: LIHTC/subsidized median canopy '
            f'<b>{bm.get("subsidized")}%</b>, NOAH <b>{bm.get("noah")}%</b>, market-rate '
            f'<b>{bm.get("market_rate")}%</b>. Yet at the <b>block-group</b> scale, canopy '
            f'correlates <b>negatively</b> with LIHTC units (Spearman '
            f'{corr["rho"]:+.2f}, n={corr["n"]:,}).</p>')
        html.append(
            '<p style="font-size:13px;color:#333">The reconciliation is siting. LIHTC '
            f'concentrates in denser, more urban block groups that are lower-canopy '
            f'overall — block groups <b>with</b> LIHTC average <b>{p["bg_canopy_with_lihtc"]}%</b> '
            f'canopy, those <b>without</b> average <b>{p["bg_canopy_without_lihtc"]}%</b>. So '
            'across neighborhoods, more LIHTC tracks less canopy; but <i>within</i> those '
            'neighborhoods, the affordable building itself is not the least-green parcel. '
            'This is an ecological (areal-aggregation) difference, not a contradiction.</p>')
        # side-by-side bars
        html.append('<div style="display:flex;gap:30px;margin:14px 0">')
        html.append('<div><div style="font-weight:600;font-size:12px;margin-bottom:4px">'
                    'Building median canopy %</div>')
        bmax = max(v for v in bm.values() if v) or 1
        for t in TYPE_ORDER:
            val = bm.get(t) or 0
            html.append(f'<div style="display:flex;align-items:center;margin:2px 0">'
                        f'<div style="width:90px;font-size:12px">{TYPE_LABELS[t].split(" (")[0]}</div>'
                        f'<div style="width:{int(150 * val / bmax)}px;height:13px;'
                        f'background:{TYPE_COLORS[t]}"></div>'
                        f'<span style="margin-left:6px;font-size:12px">{val}%</span></div>')
        html.append('</div>')
        html.append('<div><div style="font-weight:600;font-size:12px;margin-bottom:4px">'
                    'Block-group mean canopy %</div>')
        for lbl, val, col in [("BGs with LIHTC", p["bg_canopy_with_lihtc"], "#0072B2"),
                              ("BGs without", p["bg_canopy_without_lihtc"], "#999")]:
            html.append(f'<div style="display:flex;align-items:center;margin:2px 0">'
                        f'<div style="width:110px;font-size:12px">{lbl}</div>'
                        f'<div style="width:{int(150 * val / 40)}px;height:13px;background:{col}"></div>'
                        f'<span style="margin-left:6px;font-size:12px">{val}%</span></div>')
        html.append('</div></div>')
        if rv:
            html.append(
                '<p style="font-size:13px;color:#333;margin-top:12px"><b>Rent check on the '
                'NOAH label.</b> The NOAH label is set from assessed value, but observed '
                'rents agree: NOAH buildings sit in block groups where a median '
                f'<b>{rv.get("noah")}%</b> of renter units rent at or below the 60%-AMI '
                f'line, vs <b>{rv.get("market_rate")}%</b> for market-rate — evidence the '
                'value cutoff is picking out genuinely affordable-rent locations.</p>')
        html.append('</div>')
        return ui.HTML("".join(html))


app = App(app_ui, server)

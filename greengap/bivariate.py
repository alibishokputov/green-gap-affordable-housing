"""Helpers for building bivariate choropleth maps.

A bivariate choropleth encodes *two* variables at once by crossing an N-class
classification of each variable into an N x N color grid. This module provides:

- ``BIVARIATE_PALETTES`` : ready-made 3x3 color grids.
- ``bivariate_classes`` : assign each row a (row, col) class from two columns.
- ``bivariate_colors``  : map those classes to hex colors.
- ``bivariate_legend_figure`` : a small matplotlib legend swatch for the grid.

The colors work with any mapping backend (folium, geopandas ``.explore``,
matplotlib, plotly) because the output is just a per-row hex color column.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 3x3 palettes as a flat 9-list, row-major: index = row * 3 + col.
# row = variable A (bottom->top => low->high), col = variable B (left->right => low->high).
BIVARIATE_PALETTES: dict[str, list[str]] = {
    # Stevens "green-blue": low/low = pale, high-A = green, high-B = blue, high/high = teal.
    "GnBu": [
        "#e8e8e8", "#b5c0da", "#6c83b5",  # low A
        "#b8d6be", "#90b2b3", "#567994",  # mid A
        "#73ae80", "#5a9178", "#2a5a5b",  # high A
    ],
    # Purple-orange diverging bivariate.
    "PuOr": [
        "#e8e8e8", "#e4acac", "#c85a5a",
        "#b0d5df", "#ad9ea5", "#985356",
        "#64acbe", "#627f8c", "#574249",
    ],
}


def bivariate_classes(
    df: pd.DataFrame,
    col_a: str,
    col_b: str,
    n: int = 3,
    scheme: str = "quantiles",
) -> pd.DataFrame:
    """Return ``df`` with integer class columns ``_bi_a`` / ``_bi_b`` in ``[0, n)``.

    ``scheme`` is passed to :mod:`mapclassify` (e.g. ``"quantiles"``,
    ``"equal_interval"``, ``"natural_breaks"``). Rows with NaN in either
    variable get class 0.
    """
    import mapclassify

    out = df.copy()
    for src, dst in ((col_a, "_bi_a"), (col_b, "_bi_b")):
        values = out[src].to_numpy(dtype="float64")
        valid = ~np.isnan(values)
        classes = np.zeros(len(out), dtype="int64")
        if valid.sum() >= n:
            classifier = mapclassify.classify(values[valid], scheme, k=n)
            classes[valid] = classifier.yb
        out[dst] = classes
    return out


def bivariate_colors(
    df: pd.DataFrame,
    col_a: str,
    col_b: str,
    n: int = 3,
    scheme: str = "quantiles",
    palette: str = "GnBu",
) -> pd.Series:
    """Return a Series of hex colors for a bivariate map of ``col_a`` x ``col_b``."""
    if n != 3:
        raise ValueError("Built-in palettes are 3x3; pass n=3.")
    colors = BIVARIATE_PALETTES[palette]
    classed = bivariate_classes(df, col_a, col_b, n=n, scheme=scheme)
    idx = classed["_bi_a"].to_numpy() * n + classed["_bi_b"].to_numpy()
    return pd.Series([colors[i] for i in idx], index=df.index, name="bi_color")


def bivariate_legend_figure(
    palette: str = "GnBu",
    label_a: str = "Variable A",
    label_b: str = "Variable B",
):
    """Return a small matplotlib ``Figure`` showing the 3x3 legend swatch."""
    import matplotlib.pyplot as plt

    colors = BIVARIATE_PALETTES[palette]
    fig, ax = plt.subplots(figsize=(2.6, 2.6))
    for row in range(3):
        for col in range(3):
            ax.add_patch(
                plt.Rectangle((col, row), 1, 1, facecolor=colors[row * 3 + col])
            )
    ax.set_xlim(0, 3)
    ax.set_ylim(0, 3)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel(f"{label_b}  →", fontsize=9)
    ax.set_ylabel(f"{label_a}  →", fontsize=9)
    ax.set_aspect("equal")
    fig.tight_layout()
    return fig

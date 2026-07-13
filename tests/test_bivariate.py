import numpy as np
import pandas as pd

from greengap.bivariate import (
    BIVARIATE_PALETTES,
    bivariate_classes,
    bivariate_colors,
)


def _demo_frame(n=90, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "green_share": rng.random(n),
            "afh_units": rng.random(n) * 300,
        }
    )


def test_classes_in_range():
    df = _demo_frame()
    out = bivariate_classes(df, "green_share", "afh_units", n=3)
    assert out["_bi_a"].between(0, 2).all()
    assert out["_bi_b"].between(0, 2).all()


def test_colors_are_hex_from_palette():
    df = _demo_frame()
    colors = bivariate_colors(df, "green_share", "afh_units", palette="GnBu")
    assert len(colors) == len(df)
    valid = set(BIVARIATE_PALETTES["GnBu"])
    assert set(colors) <= valid
    assert all(c.startswith("#") and len(c) == 7 for c in colors)


def test_high_high_gets_darkest_corner():
    # Construct a frame where the last row is the max on both variables.
    df = pd.DataFrame(
        {
            "green_share": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.99],
            "afh_units": [0, 10, 20, 30, 40, 50, 60, 70, 999],
        }
    )
    colors = bivariate_colors(df, "green_share", "afh_units", palette="GnBu")
    # top-right corner of the 3x3 grid = index 2*3 + 2 = 8
    assert colors.iloc[-1] == BIVARIATE_PALETTES["GnBu"][8]


def test_nan_rows_do_not_crash():
    df = _demo_frame()
    df.loc[0, "green_share"] = np.nan
    colors = bivariate_colors(df, "green_share", "afh_units")
    assert len(colors) == len(df)

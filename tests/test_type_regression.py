import numpy as np
import pandas as pd

from greengap.type_regression import TYPE_TERMS, build_design, type_gaps


def _synthetic(n=600, noah_effect=5.0, subsidized_effect=-3.0, seed=0):
    rng = np.random.default_rng(seed)
    ht = rng.choice(["market_rate", "noah", "subsidized"], n)
    canopy = (
        20.0
        + noah_effect * (ht == "noah")
        + subsidized_effect * (ht == "subsidized")
        + rng.normal(0, 2, n)
    )
    return pd.DataFrame({
        "housing_type": ht,
        "canopy_pct": canopy,
        "state": "MD",
        "jurisdiction": rng.choice(["Montgomery", "Howard"], n),
        "lot_area": rng.uniform(500, 5000, n),
        "units": rng.uniform(5, 100, n),
        "year_built": rng.uniform(1950, 2020, n),
        "median_income": rng.uniform(40000, 150000, n),
        "poverty_rate": rng.uniform(2, 30, n),
        "pct_bachelors_plus": rng.uniform(10, 90, n),
        "pct_black_nh": rng.uniform(5, 90, n),
        "pct_hispanic": rng.uniform(2, 40, n),
        "pop_density": rng.uniform(500, 10000, n),
        "in_floodplain": 0,
    })


def test_recovers_known_type_effects():
    g = type_gaps(_synthetic(), "canopy_pct", with_controls=True).set_index("type")
    assert abs(g.loc["noah", "gap_vs_market"] - 5.0) < 1.0
    assert abs(g.loc["subsidized", "gap_vs_market"] - (-3.0)) < 1.0


def test_market_rate_is_the_baseline():
    # The design has type dummies for NOAH and subsidized only; market-rate is
    # implicit (the intercept), so each coefficient is a contrast against it.
    design = build_design(_synthetic(), "canopy_pct", with_controls=False)
    assert set(f"type_{t}" for t in TYPE_TERMS) <= set(design.columns)
    assert "type_market_rate" not in design.columns


def test_confidence_interval_brackets_estimate():
    g = type_gaps(_synthetic(), "canopy_pct", with_controls=True)
    for _, row in g.iterrows():
        assert row["ci_low"] <= row["gap_vs_market"] <= row["ci_high"]


def test_no_true_effect_is_not_significant():
    # With no baked-in gap, the NOAH coefficient should be small and its CI cross 0.
    g = type_gaps(_synthetic(noah_effect=0.0, subsidized_effect=0.0, seed=3), "canopy_pct",
                  with_controls=True).set_index("type")
    assert g.loc["noah", "ci_low"] <= 0 <= g.loc["noah", "ci_high"]

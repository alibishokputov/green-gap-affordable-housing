import geopandas as gpd
from shapely.geometry import Point

from greengap.housing_type import DEFAULT_NOAH_VARIANT, label_types


def _buildings(rows):
    # rows: (subsidised, value_per_unit, units)
    cols = ["subsidised", "value_per_unit", "units"]
    df = gpd.GeoDataFrame(
        [dict(zip(cols, r)) for r in rows],
        geometry=[Point(0, 0)] * len(rows),
    )
    df["building_id"] = range(len(df))
    df["state"] = "MD"
    return df


def test_subsidised_wins_regardless_of_value():
    # A subsidised building with a low value/unit is 'subsidised', not 'noah'.
    out = label_types(_buildings([(True, 40_000, 10)] + [(False, 50_000, 10)] * 9))
    assert out.loc[out["building_id"] == 0, "housing_type"].iloc[0] == "subsidised"


def test_low_value_per_unit_is_noah_high_is_market():
    # Spread values so the central-quantile cutoff separates the two clearly.
    rows = [(False, v, 10) for v in (30_000, 35_000, 40_000, 300_000, 320_000)]
    out = label_types(_buildings(rows))
    types = list(out["housing_type"])
    assert "noah" in types and "market_rate" in types
    # cheapest is NOAH, priciest is market-rate
    assert out.sort_values("value_per_unit")["housing_type"].iloc[0] == "noah"
    assert out.sort_values("value_per_unit")["housing_type"].iloc[-1] == "market_rate"


def test_value_per_unit_ceiling_is_unknown_not_market():
    # A multi-million value/unit means the unit count is untrustworthy -> unknown.
    out = label_types(_buildings([(False, 9_000_000, 1)] + [(False, 50_000, 10)] * 9))
    assert out.loc[out["building_id"] == 0, "housing_type"].iloc[0] == "unknown"


def test_missing_value_per_unit_is_unknown():
    out = label_types(_buildings([(False, None, 10)] + [(False, 50_000, 10)] * 9))
    assert out.loc[out["building_id"] == 0, "housing_type"].iloc[0] == "unknown"


def test_three_threshold_variants_are_nested():
    # Strict (25th pct) NOAH set must be a subset of broad (50th pct).
    rows = [(False, v, 10) for v in range(20_000, 220_000, 20_000)]
    out = label_types(_buildings(rows))
    strict = set(out.loc[out["noah_strict"] == True, "building_id"])  # noqa: E712
    broad = set(out.loc[out["noah_broad"] == True, "building_id"])  # noqa: E712
    assert strict <= broad
    assert f"noah_{DEFAULT_NOAH_VARIANT}" in out.columns

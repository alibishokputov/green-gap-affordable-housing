import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from greengap.housing_type import DEFAULT_NOAH_VARIANT, label_types


def _buildings(rows):
    # rows: (subsidized, value_per_unit, units)
    cols = ["subsidized", "value_per_unit", "units"]
    df = gpd.GeoDataFrame(
        [dict(zip(cols, r)) for r in rows],
        geometry=[Point(0, 0)] * len(rows),
    )
    df["building_id"] = range(len(df))
    df["state"] = "MD"
    df["jurisdiction"] = "Montgomery"
    return df


def _parcels():
    # A synthetic sales frame whose assessed/sale ratio is ~1.0, so the AMI cutoff
    # is driven by the AMI + GRM chain, not a distorted assessment ratio.
    return gpd.GeoDataFrame(
        {
            "state": ["MD"] * 20,
            "assessed_total": [100_000] * 20,
            "sale_price": [100_000] * 20,
            "sale_date": pd.to_datetime(["2020-01-01"] * 20),
        },
        geometry=[Point(0, 0)] * 20,
    )


def test_subsidized_wins_regardless_of_value():
    out = label_types(_buildings([(True, 40_000, 10)] + [(False, 50_000, 10)] * 9), _parcels())
    assert out.loc[out["building_id"] == 0, "housing_type"].iloc[0] == "subsidized"


def test_below_ami_cutoff_is_noah_above_is_market():
    # Montgomery ami60 cutoff ~ $148k at ratio 1 it is 88050*0.75*0.30*10 ~ $198k.
    # A cheap building is NOAH, a $500k/unit building is market-rate.
    out = label_types(_buildings([(False, 60_000, 10), (False, 500_000, 10)]), _parcels())
    s = out.sort_values("value_per_unit")
    assert s["housing_type"].iloc[0] == "noah"
    assert s["housing_type"].iloc[-1] == "market_rate"


def test_value_per_unit_ceiling_is_unknown_not_market():
    out = label_types(_buildings([(False, 9_000_000, 1)] + [(False, 50_000, 10)] * 9), _parcels())
    assert out.loc[out["building_id"] == 0, "housing_type"].iloc[0] == "unknown"


def test_missing_value_per_unit_is_unknown():
    out = label_types(_buildings([(False, None, 10)] + [(False, 50_000, 10)] * 9), _parcels())
    assert out.loc[out["building_id"] == 0, "housing_type"].iloc[0] == "unknown"


def test_ami_variants_are_nested():
    # A stricter AMI level (ami50) NOAH set is a subset of a looser one (ami80):
    # the cutoff rises with the AMI level, so anything NOAH at 50% is NOAH at 80%.
    rows = [(False, v, 10) for v in range(60_000, 320_000, 20_000)]
    out = label_types(_buildings(rows), _parcels())
    strict = set(out.loc[out["noah_ami50"] == True, "building_id"])  # noqa: E712
    loose = set(out.loc[out["noah_ami80"] == True, "building_id"])  # noqa: E712
    assert strict <= loose
    assert f"noah_{DEFAULT_NOAH_VARIANT}" in out.columns

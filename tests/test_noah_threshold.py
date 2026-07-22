import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from greengap import noah_threshold as nt


def _parcels(ratio=1.0, n=30):
    # assessed = ratio * sale, so the measured assessment ratio equals `ratio`.
    return gpd.GeoDataFrame(
        {
            "state": ["MD"] * n,
            "assessed_total": [100_000 * ratio] * n,
            "sale_price": [100_000] * n,
            "sale_date": pd.to_datetime(["2020-06-01"] * n),
        },
        geometry=[Point(0, 0)] * n,
    )


def test_assessment_ratio_recovers_the_input():
    r = nt.estimate_assessment_ratio(_parcels(ratio=0.8))
    assert abs(r["MD"] - 0.8) < 0.01


def test_cutoff_follows_the_affordability_chain():
    # cutoff = AMI(level) * 0.30 * GRM * assessment_ratio, by construction.
    level = 0.60
    ratio = 0.75
    cut = nt.affordable_assessed_value_per_unit("Montgomery", "MD", ratio, level)
    ami = nt.ami_income("Montgomery", "MD", level)
    expected = ami * nt.RENT_INCOME_SHARE * nt.GROSS_RENT_MULTIPLIER * ratio
    assert abs(cut - expected) < 1.0


def test_higher_ami_level_gives_higher_cutoff():
    ratio = 0.9
    c50 = nt.affordable_assessed_value_per_unit("District of Columbia", "DC", ratio, 0.50)
    c80 = nt.affordable_assessed_value_per_unit("District of Columbia", "DC", ratio, 0.80)
    assert c50 < c80


def test_cutoff_map_covers_every_study_jurisdiction():
    cuts = nt.cutoff_map(_parcels(), nt.AMI_LEVELS[nt.DEFAULT_AMI_LEVEL])
    assert set(cuts) == set(nt.AMI80_3PERSON)
    assert all(v > 0 for v in cuts.values())

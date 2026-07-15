import pandas as pd
import pytest

from greengap.dataset import LC_CLASSES, derive_canopy_columns


def _frac_row(**shares: float) -> pd.DataFrame:
    """Build a one-row class-fraction frame; unnamed classes get 0.0."""
    row = {f"frac_{name}": 0.0 for name in LC_CLASSES.values()}
    for key, value in shares.items():
        row[f"frac_{key}"] = value
    return pd.DataFrame([row])


def test_canopy_sums_all_four_canopy_classes():
    # 10% each of plain canopy + the three "canopy over impervious" classes.
    df = _frac_row(
        tree_canopy=0.10,
        tree_canopy_over_structures=0.10,
        tree_canopy_over_other_impervious=0.10,
        tree_canopy_over_impervious_roads=0.10,
        low_vegetation=0.60,
    )
    out = derive_canopy_columns(df)
    assert out["canopy_pct"].iloc[0] == pytest.approx(40.0)
    # Natural-only counts just class 3 - the definitional choice that matters.
    assert out["natural_canopy_pct"].iloc[0] == pytest.approx(10.0)


def test_water_excluded_from_land_denominator():
    # Half water, and every land pixel is canopy -> 100% of LAND is canopy...
    df = _frac_row(water=0.5, tree_canopy=0.5)
    out = derive_canopy_columns(df)
    assert out["canopy_pct"].iloc[0] == pytest.approx(100.0)
    # ...but only 50% of the tract's total area.
    assert out["canopy_pct_total"].iloc[0] == pytest.approx(50.0)
    assert out["water_pct"].iloc[0] == pytest.approx(50.0)


def test_all_water_tract_does_not_divide_by_zero():
    df = _frac_row(water=1.0)
    out = derive_canopy_columns(df)
    assert out["canopy_pct"].iloc[0] == pytest.approx(0.0)
    assert out["water_pct"].iloc[0] == pytest.approx(100.0)


def test_partial_raster_coverage_uses_classified_area_as_denominator():
    # A tract straddling the raster edge: only 50% classified, and half of THAT
    # is canopy. Real cases exist in MD (one tract is 52% covered). Dividing by a
    # hard-coded 1.0 would report 25% and silently halve the tract's canopy.
    df = _frac_row(tree_canopy=0.25, low_vegetation=0.25)
    out = derive_canopy_columns(df)
    assert out["data_coverage"].iloc[0] == pytest.approx(0.5)
    assert out["canopy_pct"].iloc[0] == pytest.approx(50.0)


def test_partial_coverage_with_water_excludes_both_water_and_nodata():
    # 40% classified: 10% water, 30% canopy -> land = 30%, all of it canopy.
    df = _frac_row(water=0.10, tree_canopy=0.30)
    out = derive_canopy_columns(df)
    assert out["data_coverage"].iloc[0] == pytest.approx(0.40)
    assert out["canopy_pct"].iloc[0] == pytest.approx(100.0)  # % of land
    assert out["canopy_pct_total"].iloc[0] == pytest.approx(75.0)  # % of classified
    assert out["water_pct"].iloc[0] == pytest.approx(25.0)


def test_no_canopy_is_zero_not_nan():
    df = _frac_row(impervious_roads=0.7, low_vegetation=0.3)
    out = derive_canopy_columns(df)
    assert out["canopy_pct"].iloc[0] == pytest.approx(0.0)
    assert out["canopy_pct"].notna().all()

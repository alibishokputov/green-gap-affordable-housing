import numpy as np
import pandas as pd

from greengap.rent import BG_RENTER_DENOM, RENT_BRACKETS, affordable_share


def _bg(counts, denom):
    row = {var: 0 for var, _ in RENT_BRACKETS}
    row.update(counts)
    row[BG_RENTER_DENOM] = denom
    return pd.DataFrame([{k: str(v) for k, v in row.items()}])


def test_all_units_below_line_is_100pct():
    # 100 units in the $250-299 bracket (upper 300), all below a $1,650 line.
    df = _bg({"B25063_007E": 100}, denom=100)
    assert affordable_share(df, 1650).iloc[0] == 100.0


def test_all_units_above_line_is_zero():
    # 100 units in the $2,000-2,499 bracket, all above a $1,650 line.
    df = _bg({"B25063_023E": 100}, denom=100)
    assert affordable_share(df, 1650).iloc[0] == 0.0


def test_bracket_interpolation_is_linear():
    # 100 units in the $1,500-1,999 bracket. A $1,650 line sits 30% into it
    # ((1650-1500)/(2000-1500)), so ~30 units are counted affordable.
    df = _bg({"B25063_022E": 100}, denom=100)
    share = affordable_share(df, 1650).iloc[0]
    assert abs(share - 30.0) < 0.5


def test_half_below_half_above():
    df = _bg({"B25063_007E": 50, "B25063_023E": 50}, denom=100)
    assert affordable_share(df, 1650).iloc[0] == 50.0


def test_zero_denominator_is_nan():
    df = _bg({"B25063_007E": 0}, denom=0)
    assert np.isnan(affordable_share(df, 1650).iloc[0])

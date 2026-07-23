import geopandas as gpd
import numpy as np
from shapely.geometry import Point

from greengap import type_regression_data as trd


def _frame(n=600, seed=0):
    """Synthetic building frame: NOAH buildings carry higher canopy by construction."""
    rng = np.random.default_rng(seed)
    ht = rng.choice(["subsidized", "noah", "market_rate"], n)
    canopy = 15.0 + 8.0 * (ht == "noah") - 4.0 * (ht == "market_rate") + rng.normal(0, 2, n)
    lst = 40.0 - 0.5 * (ht == "noah") + rng.normal(0, 1, n)
    geoids = rng.choice([f"2403100{i:04d}" for i in range(40)], n)
    return gpd.GeoDataFrame(
        {
            "housing_type": ht,
            "GEOID": geoids,
            "units": rng.integers(5, 100, n).astype(float),
            "canopy_pct": canopy,
            "natural_canopy_pct": canopy - 1,
            "mean_lst": lst,
            "mean_ndvi": 0.3 + 0.01 * canopy + rng.normal(0, 0.02, n),
        },
        geometry=[Point(0, 0)] * n,
    )


def _fake_bg(geoids):
    # A block-group table with an env value per GEOID, matching the frame's GEOIDs.
    rng = np.random.default_rng(1)
    u = sorted(set(geoids))
    return gpd.GeoDataFrame(
        {
            "GEOID": u,
            "canopy_pct": rng.uniform(5, 45, len(u)),
            "natural_canopy_pct": rng.uniform(5, 45, len(u)),
            "mean_lst": rng.uniform(38, 43, len(u)),
            "mean_ndvi": rng.uniform(0.2, 0.6, len(u)),
        },
        geometry=[Point(0, 0)] * len(u),
    )


def _patched(monkeypatch, frame):
    fake = _fake_bg(frame["GEOID"])
    monkeypatch.setattr(trd.gpd, "read_parquet", lambda *a, **k: fake)
    return trd._type_correlations(frame)


def test_building_pointbiserial_recovers_sign(monkeypatch):
    out = _patched(monkeypatch, _frame())
    # NOAH was built greener, market-rate less green.
    assert out["building"]["noah"]["canopy_pct"]["r"] > 0.2
    assert out["building"]["market_rate"]["canopy_pct"]["r"] < 0


def test_all_cells_present_and_well_formed(monkeypatch):
    out = _patched(monkeypatch, _frame())
    for scale in ("building", "block_group"):
        for t in trd.CORR_TYPES:
            for m in trd.ENV_MEASURES:
                cell = out[scale][t][m]
                if cell is not None:
                    assert set(cell) == {"r", "p", "n"}
                    assert -1 <= cell["r"] <= 1


def test_no_object_dtype_crash(monkeypatch):
    # The block-group share is a ratio with a possibly-NA denominator; this used to
    # produce an object-dtype array that scipy could not consume. A frame where some
    # block groups have zero total units exercises that path.
    f = _frame()
    f.loc[f.index[:50], "units"] = 0.0
    out = _patched(monkeypatch, f)
    assert "block_group" in out  # completed without raising

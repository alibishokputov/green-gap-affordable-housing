import geopandas as gpd
from shapely.geometry import Point, box

from greengap import parcel_env


def test_flood_null_when_no_nfhl(monkeypatch, tmp_path):
    # With no NFHL layer present, flood is left null (not silently False).
    monkeypatch.setattr(parcel_env, "FEMA_NFHL", tmp_path / "absent.gpkg")
    b = gpd.GeoDataFrame({"building_id": [1, 2]}, geometry=[Point(0, 0), Point(1, 1)],
                         crs=parcel_env.CORRIDOR_CRS)
    out = parcel_env.compute_flood(b)
    assert out["in_floodplain"].isna().all()


def test_flood_flags_only_intersecting_buildings(monkeypatch, tmp_path):
    # One high-risk zone polygon; only the building inside it is flagged.
    nfhl = gpd.GeoDataFrame(
        {"FLD_ZONE": ["AE"]}, geometry=[box(0, 0, 10, 10)], crs=parcel_env.CORRIDOR_CRS
    )
    path = tmp_path / "fema_nfhl.gpkg"
    nfhl.to_file(path, driver="GPKG")
    monkeypatch.setattr(parcel_env, "FEMA_NFHL", path)

    b = gpd.GeoDataFrame(
        {"building_id": [1, 2]},
        geometry=[box(1, 1, 2, 2), box(50, 50, 51, 51)],  # inside, outside
        crs=parcel_env.CORRIDOR_CRS,
    )
    out = parcel_env.compute_flood(b)
    flagged = set(out.loc[out["in_floodplain"], "building_id"])
    assert flagged == {1}


def test_flood_ignores_low_risk_zones(monkeypatch, tmp_path):
    # Zone X (minimal risk) is not in FEMA_HIGH_RISK_ZONES -> no building flagged.
    nfhl = gpd.GeoDataFrame(
        {"FLD_ZONE": ["X"]}, geometry=[box(0, 0, 10, 10)], crs=parcel_env.CORRIDOR_CRS
    )
    path = tmp_path / "fema_nfhl.gpkg"
    nfhl.to_file(path, driver="GPKG")
    monkeypatch.setattr(parcel_env, "FEMA_NFHL", path)

    b = gpd.GeoDataFrame({"building_id": [1]}, geometry=[box(1, 1, 2, 2)],
                         crs=parcel_env.CORRIDOR_CRS)
    out = parcel_env.compute_flood(b)
    assert not out["in_floodplain"].any()

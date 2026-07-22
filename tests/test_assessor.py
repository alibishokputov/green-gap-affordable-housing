import geopandas as gpd
from shapely.geometry import Point

from greengap.assessor import flag_dc_multifamily, flag_md_multifamily


def _md_frame(rows):
    cols = ["LU", "STRUBLDG", "BLDG_UNITS"]
    df = gpd.GeoDataFrame(
        [dict(zip(cols, r)) for r in rows], geometry=[Point(0, 0)] * len(rows)
    )
    return df


def test_md_land_use_apartment_is_multifamily():
    # LU == 'M' qualifies regardless of unit count or structure code.
    kept = flag_md_multifamily(_md_frame([("M", "0001", 1)]))
    assert len(kept) == 1


def test_md_structure_code_needs_unit_floor():
    # C101 (Apartment) qualifies only at >= 5 units; a 3-unit building does not.
    frame = _md_frame([("R", "C101", 20), ("R", "C101", 3)])
    kept = flag_md_multifamily(frame)
    assert list(kept.index) == [0]


def test_md_plain_residential_dropped():
    kept = flag_md_multifamily(_md_frame([("R", "0001", 1)]))
    assert kept.empty


def _dc_frame(codes):
    return gpd.GeoDataFrame({"USECODE": codes}, geometry=[Point(0, 0)] * len(codes))


def test_dc_core_codes_kept():
    kept = flag_dc_multifamily(_dc_frame(["021", "022", "029", "041"]))
    assert list(kept["USECODE"]) == ["021", "022", "029"]


def test_dc_broad_adds_conversions_and_coops():
    frame = _dc_frame(["021", "024", "041"])
    narrow = flag_dc_multifamily(frame)
    broad = flag_dc_multifamily(frame, broad=True)
    assert list(narrow["USECODE"]) == ["021"]
    assert list(broad["USECODE"]) == ["021", "024"]


def test_dc_condominium_code_excluded():
    # 016 is a residential condo (per-unit ownership), never rental multifamily.
    assert flag_dc_multifamily(_dc_frame(["016"])).empty

# Assessor Harmonisation: Field Mapping and Multifamily Crosswalk

Companion to `tax-assessor-extraction.md`. Documents what the `greengap.assessor`
pipeline actually built: the sources used, the field mapping from each native
schema to the common parcel schema, the multifamily use-code crosswalk, and the
QA outcome. This is the appendix deliverable referenced in the extraction plan.

## Output

`data/processed/parcels_mf.parquet` — one row per multifamily rental parcel,
projected to NAD83 Maryland State Plane (EPSG:26985).

| | Parcels | With geometry | With assessed value |
|---|---:|---:|---:|
| Maryland (6 jurisdictions) | 7,558 | 100% | 99.1% |
| District of Columbia | 3,531 | 100% | 99.8% |
| **Total** | **11,089** | **100%** | — |

MD by jurisdiction: Baltimore County 2,594; Baltimore City 2,366; Montgomery
1,129; Prince George's 992; Anne Arundel 285; Howard 192.

## Sources

Both jurisdictions turned out to publish a single parcel-fabric layer carrying
assessment values, characteristics, and geometry together, so the four-way
Core+Building+Land join sketched in the extraction plan was unnecessary.

**Maryland** — MdProperty View statewide geodatabase, February 2026 bundle
(`data/raw/February_2026_Parcels.zip`, layer `parcel_polygons`). SDAT assessment
fields are inline with parcel geometry. Keyed on `ACCTID`. Subset to the six study
jurisdictions by `JURSCODE ∈ {BACI, BACO, ANNE, HOWA, MONT, PRIN}`.

**District of Columbia** — Open Data DC, `DCGIS_DATA/Property_and_Land` MapServer,
fetched via the REST query API (snapshot 2026-07-21). Keyed on `SSL` (string;
never cast to numeric — suffixes are directional). Layers used:

| Layer | REST id | Rows | Role |
|---|---:|---:|---|
| Owner Polygons (Common Ownership Layer) | 40 | 137,400 | geometry + assessed values + use code (hub) |
| COMMERCIAL (CAMA) | 23 | 21,182 | multifamily building characteristics |
| USECODE | 54 | 109 | use-code labels |
| PROPERTY SALES (CAMA) | 57 | 421,370 | full sales history (not yet joined) |

Two corrections to the extraction plan on the DC side:

1. **Assessed values are not in CAMA.** The three CAMA files (Commercial /
   Residential / Condominium) carry building characteristics and the last sale
   only. Current assessed land/improvement/total are in **Owner Polygons**
   (`NEWLAND / NEWIMPR / NEWTOTAL`), the ITSPE ledger published inline on the
   common-ownership polygons.
2. **Tax Lots (id 39) is the wrong geometry layer.** It is a recordation layer
   that matches only ~14% of assessed SSLs (~29% of multifamily). **Owner
   Polygons** is the assessment-parcel fabric and matches essentially all of them.
   Joining assessments to Tax Lots left 71% of DC multifamily parcels with no
   geometry; Owner Polygons brings that to 0%.

## Field mapping

Common schema ← MD source ← DC source. Values are the "new full market" /
"new" assessed figures in each system (the current-cycle assessment).

| Target | Maryland (MdProperty View) | DC (Owner Polygons / CAMA) | Notes |
|---|---|---|---|
| `parcel_uid` | `"MD:" + ACCTID` | `"DC:" + SSL` | native key kept in `native_key` |
| `jurisdiction` | `JURSCODE` → name | "District of Columbia" | |
| `state` | "MD" | "DC" | |
| `assessed_land` | `NFMLNDVL` | `NEWLAND` | USD |
| `assessed_improvement` | `NFMIMPVL` | `NEWIMPR` | USD |
| `assessed_total` | `NFMTTLVL` | `NEWTOTAL` | USD |
| `year_built` | `YEARBLT` | `AYB` (CAMA Commercial) | see limitation below |
| `units` | `BLDG_UNITS` | `NUM_UNITS` (CAMA Commercial) | dwelling units |
| `building_area` | `SQFTSTRC` | `LIVING_GBA` (CAMA Commercial) | ft² |
| `lot_area` | `LANDAREA` | `LANDAREA` | ft² |
| `use_code` | `LU` | `USECODE` | native scheme, not comparable across states |
| `use_desc` | `DESCLU` | USECODE lookup | |
| `sale_price` | `CONSIDR1` | `SALEPRICE` | last-sale consideration |
| `sale_date` | `TRADATE` | `SALEDATE` | |
| `geometry` | parcel polygon | Owner-Polygon | reprojected to EPSG:26985 |

## Multifamily crosswalk

The two use-code schemes are unrelated. The multifamily-rental set was hand-built
against each and is kept narrow by default, with a documented broad variant for
sensitivity (`build --broad-dc`). Condominium codes are per-unit ownership, not
rental stock, and are excluded from both sides.

**Maryland.** Primary signal is the SDAT land-use code `LU = 'M'` ("Apartments",
7,234 parcels). A secondary structure-code path adds apartment stock that sits
under a non-`M` land use, gated by a 5-unit floor to keep out small 2–4 unit
buildings (324 parcels):

| Signal | Codes | |
|---|---|---|
| Land use `LU` | `M` | Apartments |
| Structure `STRUBLDG` (units ≥ 5) | `C101` | Apartment |
| | `C113` | Multiple Residence |
| | `C307` | Residential Apartment Units |
| | `C179` | Mixed Residential / Retail |

**District of Columbia.** Purpose-built rental apartments plus generic
multifamily (3,531 parcels):

| Set | Codes | |
|---|---|---|
| Core (default) | `021` | Residential-Apartment-Walk-Up (6+ units) |
| | `022` | Residential-Apartment-Elevator (12+ units) |
| | `029` | Residential-Multifamily, Misc |
| | `002` | Residential-Multi-Family (NC) — legacy, 0 current rows |
| Broad (`--broad-dc`) | `023,024,025` | Flats <5 / conversions |
| | `026,027,028` | Cooperatives |
| | `214` | Garage-Multi-Family |

The broad set is dominated by code `024` (Conversions <5 units, ~7,500 parcels),
which are mostly small 2–4 unit properties. Whether they belong in "multifamily
rental" is a definitional choice the sensitivity run exists to test; they are out
of the core set.

## QA outcome

- Duplicate `parcel_uid`: 0.
- `assessed_total ≈ land + improvement`: holds within $1 across both states.
- Geometry: all MultiPolygon, EPSG:26985, no zero/absurd areas.
- Tax-exempt / zero-value parcels: value fields nulled (74 parcels) so placeholder
  zeros never enter the value distribution; rows retained.

Two data-quality asymmetries to carry into any cross-jurisdiction comparison:

1. **`year_built` is 68% null on the Maryland side** but only 2% null on the DC
   side. `YEARBLT` in MdProperty View is populated chiefly for single dwellings,
   not apartment parcels; DC's `AYB` comes from CAMA. Age comparisons across the
   two states are not on equal footing and should be treated cautiously or
   sourced differently for MD.
2. **Multi-parcel properties.** A single apartment complex can span several
   `ACCTID`s or `SSL`s (garages, common areas, phased buildings sharing the
   apartment land use). ~2,237 of the 11,089 MF parcels report 1 unit — complex
   sub-parcels, not standalone buildings. This is resolved downstream by the
   building aggregation in `greengap.housing_type` (below), which must run before
   any per-unit value is interpreted.

## Reproduce

```
uv run python -m greengap.assessor dc-fetch   # one-time DC REST pull
uv run python -m greengap.assessor build      # -> data/processed/parcels_mf.parquet
uv run python -m greengap.assessor build --broad-dc   # widen DC multifamily
```

---

# Housing-type labelling and parcel-scale environment

Built on `parcels_mf.parquet` in two further modules. Output tables:
`data/processed/mf_buildings_labelled.parquet` (typed buildings) and
`mf_buildings_env.parquet` (+ canopy / LST / flood).

## Building aggregation (`greengap.housing_type`, stage 1)

Multi-parcel complexes are dissolved to physical buildings by **Queen contiguity
within jurisdiction**: touching same-jurisdiction MF parcels form one building;
values/units/areas are summed, `year_built` is the max, use code is the mode.
11,089 parcels → **5,364 buildings** (1,636 span >1 parcel). This collapses the
1-unit sub-parcel problem (2,237 → 224) so value/unit becomes meaningful.

Owner is *not* used to merge: MD's `OWNADD1` (SDAT withholds owner names) and DC's
`OWNERNAME` are frequently shared across unrelated properties (management
companies), which would over-merge. Contiguity targets the actual failure mode.

Residual guard: a building whose value/unit exceeds **$2,000,000** has its value
and unit count on separate, non-contiguous parcels that did not merge; its unit
count is untrustworthy, so it is typed `unknown`, not market-rate (104 buildings).

## Subsidy flag (stage 2)

A building is `subsidised` if its footprint is within **30 m** of an active
LIHTC or NHPD point:

| Source | File | Filter |
|---|---|---|
| HUD LIHTC (national) | `data/raw/lihtc.zip` | MD + DC, geocoded |
| NHPD (national) | `data/external/National Housing Properties (1).xlsx` | MD + DC, `PropertyStatus = Active` |

NHPD is filtered to **active** subsidies: an expired NHPD property has lost its
subsidy and is, if anything, NOAH again. Result: 865 subsidised buildings
(MD 17.2% is a corrected figure — an earlier MD-only NHPD run understated DC).

## NOAH vs market-rate (stage 3)

Among unsubsidised, placeable buildings, `housing_type` splits on assessed value
per unit at a **per-state quantile** (DC values run higher, so a single dollar cut
would misclassify one state). Three variants are stored for sensitivity:

| Variant | Quantile | MD cutoff | DC cutoff |
|---|---|---|---|
| strict | 0.25 | ~$68k | ~$112k |
| central (default) | 0.40 | ~$90k | ~$143k |
| broad | 0.50 | ~$105k | ~$169k |

Condominium codes are already excluded upstream. Final labels (central variant):
**subsidised 865, NOAH 1,660, market-rate 2,491, unknown 348.**

## Parcel-scale environment (`greengap.parcel_env`)

Per building footprint, using the same rasters and canopy arithmetic as the areal
pipeline (so a building canopy % and a block-group canopy % differ only in the
polygon): `canopy_pct`, `natural_canopy_pct` (Chesapeake 1 m); `mean_lst`,
`mean_ndvi`, `mean_ndbi` (summer Landsat 30 m); `in_floodplain` (FEMA NFHL
overlay, active only when `data/external/fema_nfhl.gpkg` is present — **not yet
supplied**). `--buffer N` extracts over a ring around the footprint for green
context rather than the roof alone.

Descriptive finding to handle carefully: the NOAH-vs-market environmental contrast
is **heterogeneous and opposite-signed across states**. In DC, NOAH buildings sit
at *higher* canopy (15% vs 5.5%) and *lower* summer LST than market-rate; in MD the
two are near-identical. This is associational (siting, age, neighbourhood
unadjusted) and is a direct caution against pooling MD and DC.

## Reproduce (housing type + environment)

```
uv run python -m greengap.housing_type build          # -> mf_buildings_labelled.parquet
uv run python -m greengap.parcel_env                   # -> mf_buildings_env.parquet
uv run python -m greengap.parcel_env --buffer 30       # footprint + 30 m ring
uv run python -m greengap.parcel_env export-dashboard  # app/buildings_types.geojson
uv run shiny run app/housing_app.py                    # local dashboard
```

# Data and Methods: Green Infrastructure and Affordable Housing

Draft documentation for the data section of the paper. It records the provenance,
processing, and construction of every variable currently used in the analysis and
the interactive dashboard, together with the measurement decisions and limitations
that bear on interpretation. The scope here is the two jurisdictions assembled so
far, Maryland and Washington, DC, and one housing type, Low-Income Housing Tax
Credit (LIHTC) properties. The eventual design spans a six-jurisdiction corridor
and additional housing types (market-rate, naturally occurring affordable housing,
other assisted units); those are not yet in the data and are noted where their
absence shapes what the present measures can support.

Everything documented here is cross-sectional and describes where affordable
housing and environmental conditions co-locate. None of it identifies a causal
relationship or a between-type disparity, because the analysis currently contains
no comparison housing group. The measurement choices below are written with that
limit in view.

---

## 1. Study area and units of analysis

The study area is the land area of Maryland (state FIPS 24) and the District of
Columbia (FIPS 11). All variables are aggregated to U.S. Census areal units drawn
from the 2022 TIGER/Line shapefiles.

Two geographies are carried in parallel:

- **Census block group** (the default unit; 4,650 units: 4,079 in MD, 571 in DC).
- **Census tract** (1,681 units: 1,475 in MD, 206 in DC), retained as a coarser
  scale for sensitivity checks.

The block group is the finest standard Census geography that still carries American
Community Survey estimates, which makes it the smallest unit at which environmental
measures can later be conditioned on neighborhood socioeconomic characteristics
without moving to a modeled small-area estimate. Every aggregation step and every
cache is geography-scoped, so results can be reproduced at either scale and compared.

The choice of areal unit is consequential and is treated as a modifiable areal unit
problem (MAUP) throughout. Aggregating a 1 m land-cover raster or a 30 m temperature
surface to a block group produces an area-mean that smooths within-unit variation;
the same underlying pattern can yield different associations at tract and block-group
scale. Reported associations are checked at both scales for this reason (Section 6).

County identity is attached from the 2022 TIGER county layer using the county's
`NAMELSAD`, not `NAME`. In Maryland the independent city of Baltimore (county FIPS
510) and Baltimore County (FIPS 005) share the name "Baltimore"; joining on `NAME`
would merge two distinct jurisdictions, so the full legal name is used to keep them
separate.

---

## 2. Green infrastructure data

### 2.1 Land cover and tree canopy

**Source.** Chesapeake Bay Program (2023), *Chesapeake Bay Land Use and Land Cover
(LULC) Database, 2022 Edition*, https://doi.org/10.5066/P981GV1L. The database was
produced by the University of Vermont Spatial Analysis Laboratory with Chesapeake
Conservancy and the U.S. Geological Survey. The land-cover component is a 1 m raster
with 12 classes, delivered per jurisdiction. Maryland and DC are supplied as separate
tiles (`md_lc_2018_2022-Edition`, `dc_lc_2017_2022-Edition`).

**Land-cover classes.** The 12-class scheme, read directly from the raster value
attribute table:

| Value | Class | Value | Class |
|------:|-------|------:|-------|
| 1 | Water | 7 | Impervious Structures |
| 2 | Emergent Wetlands | 8 | Other Impervious |
| 3 | Tree Canopy | 9 | Impervious Roads |
| 4 | Scrub/Shrub | 10 | Tree Canopy over Structures |
| 5 | Low Vegetation | 11 | Tree Canopy over Other Impervious |
| 6 | Barren | 12 | Tree Canopy over Impervious Roads |

**Vintage.** The imagery underlying each tile predates the 2022 database release.
The DC tile represents a nominal 2017 land-cover epoch and the Maryland tile a
nominal 2018 epoch, with source imagery acquired across a multi-year leaf-on window.
Canopy is therefore measured several years before the 2021–2023 temperature composite
(Section 2.2) and before the current LIHTC inventory (Section 3). Canopy changes
slowly at the block-group scale, so the misalignment is modest, but it is a real
source of measurement error and is flagged again in Section 6.

**Tree canopy definition.** The four canopy classes are summed to a single urban
tree canopy (UTC) measure:

> canopy = class 3 (Tree Canopy) + class 10 (over Structures) + class 11 (over Other
> Impervious) + class 12 (over Impervious Roads)

Counting canopy that overhangs buildings, parking, and roads follows standard urban
tree canopy practice and matters for the substantive question. Restricting canopy to
class 3 alone excludes exactly the street-tree and yard-tree canopy that is most
common in dense, built-up areas, and doing so lowers measured canopy in urban
block groups by a factor of roughly two to three, the same areas where LIHTC
concentrates. Because the definition is consequential, a natural-canopy alternative
(class 3 only) is retained and can be selected in the dashboard so the analysis can
report sensitivity to the choice rather than depend on it.

**Extraction.** Class fractions are computed with `exactextract`, which intersects
each polygon with the raster and area-weights every partially covered boundary pixel,
rather than sampling pixel centroids. The Chesapeake raster is stored in an
equal-area Albers projection (ESRI:102039), so pixel counts are directly proportional
to ground area and the resulting fractions require no further area correction. For
each unit the extraction returns the fraction of area in each of the 12 classes.

**Canopy percentage and its denominator.** Two canopy percentages are constructed:

- `canopy_pct`, canopy as a share of **classified land**, with both water (class 1)
  and unclassified (nodata) area removed from the denominator.
- `canopy_pct_total`, canopy as a share of all classified area, water included.

`canopy_pct` is the primary measure. A block group that is half open water should
not read as half as green as an otherwise identical inland block group, so water is
removed from the denominator. Nodata is removed for the same reason. The class
fractions do not always sum to one: units on the shoreline or the state boundary are
partly outside the raster footprint, and, more consequentially, the Chesapeake data
masks certain military installations to nodata. Aberdeen Proving Ground (Harford
County, MD) and Joint Base Andrews (Prince George's County, MD) are the clear cases.
Dividing canopy by a fixed denominator of one, rather than by the classified area,
would understate canopy in these units by an arbitrary amount.

**Reliability flag.** Units whose classified land falls materially short of their
land area are flagged. The classified land share is compared to the Census land
share (`ALAND / (ALAND + AWATER)`); units where the gap exceeds five percentage
points, or where classified land is below one percent of the unit, are marked
`canopy_reliable = False`, and their canopy values are set to missing. At tract
scale 15 units are flagged (open-water tracts in the Census `99xxxx` series plus the
two military installations); at block-group scale 23 are flagged. These units retain
valid temperature and index values (Section 2.2) and are excluded only from
canopy-based views, never from temperature-based views.

**Validation.** As an external check, area-weighted canopy was computed for each
jurisdiction and compared to published figures. DC returns 34.9 percent canopy of
land area against a published urban tree canopy of roughly 38 percent; the shortfall
is consistent with the 2017 vintage of the DC tile, since canopy has grown since.
Maryland returns 49.8 percent. Both are within the expected range for the source and
epoch, which supports the extraction and the denominator construction.

### 2.2 Land surface temperature and spectral indices

**Source.** USGS Landsat 8 and Landsat 9, Collection 2, Level-2 science products
(surface reflectance and surface temperature), accessed through Google Earth Engine.
The two sensors are combined to increase the number of clear observations.

**Compositing window.** Summer observations (June, July, August) from 2021, 2022,
and 2023 are pooled into a three-year climatology. A single summer can be distorted
by an unusually hot or cloudy year; pooling three summers of June–August imagery and
taking the median yields a more stable estimate of typical warm-season surface
conditions. Scenes with more than 60 percent cloud cover are excluded at the scene
level before pixel-level masking. 220 scenes contribute to the composite.

**Cloud and quality masking.** Each scene is masked at the pixel level using the
Collection 2 `QA_PIXEL` band, removing pixels flagged as cloud, cloud shadow, cirrus,
or dilated cloud, and using `QA_RADSAT` to remove radiometrically saturated pixels.
Masking precedes compositing so that contaminated pixels do not enter the median.

**Scaling.** Collection 2 Level-2 scale factors are applied. Surface reflectance
bands are scaled by 0.0000275 with an offset of −0.2 and clamped to the physical
[0, 1] range. Surface temperature (`ST_B10`) is scaled by 0.00341802 with an offset
of 149.0 to Kelvin and converted to degrees Celsius.

**Per-scene indices, then median.** The spectral indices and temperature are computed
for each scene individually and only then reduced to a per-pixel median across the
three-summer stack. Taking the median of per-scene indices is the correct order of
operations; compositing raw reflectance first and computing indices from the
composite would bias the result.

The five environmental bands are:

- **Land surface temperature (LST, °C)**, from the thermal band `ST_B10`.
- **NDVI**, (NIR − Red) / (NIR + Red), a vegetation greenness index.
- **NDBI**, (SWIR1 − NIR) / (SWIR1 + NIR), a built-up / impervious index.
- **NDWI**, (Green − NIR) / (Green + NIR), a water index used mainly for quality
  screening.
- **Albedo**, broadband shortwave albedo from the Liang (2001) narrowband-to-
  broadband coefficients:
  `0.356·blue + 0.130·red + 0.373·nir + 0.085·swir1 + 0.072·swir2 − 0.0018`.

**Output grid.** The composite is exported as a five-band GeoTIFF at 30 m resolution
in UTM Zone 18N (EPSG:26918), the projection shared by all downstream layers, and
downloaded in tiles because the full stack exceeds Earth Engine's single-request
download limit. A companion layer, `CLEAR_OBS`, records the number of clear
observations contributing to each pixel's median; its median across the study area
is about 14, so the composite rests on a substantial number of clear looks rather
than one or two dates.

**What LST measures, and what it does not.** LST is the radiometric temperature of
the ground surface (the skin temperature of pavement, roofs, soil, and canopy) at the
satellite overpass, which for Landsat is mid-to-late morning. It is not air
temperature, and it is not a 24-hour or nighttime measure. Surface temperature and
air temperature are correlated but distinct; surface temperature responds more
sharply to impervious cover and shows a wider spatial range. The heat exposure that
residents experience depends additionally on humidity, wind, time of day, and indoor
conditions, none of which are captured here. LST should be read as a measure of the
daytime warm-season thermal environment of the land surface, which is the quantity
that canopy and impervious cover most directly modify.

**Native resolution of the thermal band.** The thermal instrument on Landsat 8/9
(TIRS) acquires at roughly 100 m and is resampled to the 30 m grid of the Level-2
product. The stored 30 m LST pixels therefore do not carry independent 30 m
information; the effective resolution of the temperature field is about 100 m. An
areal-unit mean spans enough native thermal pixels to be stable, but LST should not
be described or mapped as if it resolved variation at 30 m or at the scale of an
individual parcel.

### 2.3 Aggregation of environmental data to units

Both raster sources are summarized to census units with `exactextract`, which
area-weights partial boundary pixels. For land cover the summary is the set of class
fractions (Section 2.1); for the Landsat stack it is the area-weighted mean of each
band. The extraction reprojects each unit to the raster's own projection and skips
the raster's nodata, so a unit's mean reflects only valid pixels. A per-unit
`mean_clear_obs` accompanies the temperature and index means as a reliability
companion, analogous to the canopy coverage check: a unit built from few clear
observations carries a less certain median.

All aggregation is ecological. A unit's mean temperature or canopy percentage
describes the unit as a whole, not the parcel a specific housing development occupies.
Moving to a parcel- or building-level exposure measure would require the housing
footprints and a defined catchment, and is part of the planned refinement rather than
the current measure.

---

## 3. Housing data: LIHTC

**Source.** U.S. Department of Housing and Urban Development, Low-Income Housing Tax
Credit (LIHTC) database (the `LIHTCPUB` placed-in-service inventory). The analysis
reads the national file rather than the state extract that was initially supplied,
because the state extract contained only Maryland records (948 rows, none for DC) and
would silently have reduced a two-jurisdiction study to one. The national file carries
both jurisdictions with an identical schema.

**Study-area selection.** Records are restricted to projects in Maryland and DC by
the project state field, yielding 1,216 projects (948 in MD, 268 in DC).

**Geocoding and unit assignment.** Each project carries a latitude and longitude.
77 projects (6.3 percent of the study-area records) lack coordinates and cannot be
placed; they are dropped, and their exclusion is logged. Of the 1,139 geocoded
projects, 3 fall outside the census-unit layer and are dropped, leaving 1,136
projects assigned to units by a point-in-polygon spatial join. A project is assigned
to the census unit that contains its coordinate.

**Unit counts.** HUD ships imputed unit fields (`li_unitr`, `n_unitsr`) alongside the
raw fields (`li_units`, `n_units`). The raw low-income-unit field has 27 null values
in the Maryland records alone; a naive sum silently skips them and undercounts by
roughly 2,200 units. The imputed fields are complete and are the fields HUD's data
dictionary directs analysts to use, so the analysis uses them.

**Aggregation.** Projects are aggregated to each census unit as three measures: the
sum of low-income units, the sum of total units, and the count of projects. Units
with no LIHTC project are recorded as true zeros, not missing values. At block-group
scale 741 of 4,650 units contain at least one LIHTC project; at tract scale 545 of
1,681 do. The measures are strongly zero-inflated at both scales, which is addressed
in the classification of the dashboard (Section 4).

**What the LIHTC measure represents, and what it does not.** The database is a
cumulative placed-in-service inventory, so the counts describe the standing stock of
tax-credit housing, not recent construction or current occupancy. Project coordinates
are geocoded to roughly parcel-to-block precision, which is adequate for assignment to
a block group or tract but would be the binding source of error for any tighter
catchment. Assigning a project to the unit that contains its point treats the whole
unit as the project's environmental context, which is the ecological approximation
noted in Section 2.3.

Two limits are structural rather than technical. First, LIHTC is one of several
housing types the eventual design compares; on its own it has no counterfactual, so a
correlation between LIHTC location and environmental conditions describes where
tax-credit housing sits, not whether it sits in worse conditions than the market-rate
or other-assisted alternatives available on the same land. Second, LIHTC siting is
shaped by land cost, zoning, qualified-census-tract incentives, and developer
behavior, all of which also correlate with urban location and with environmental
conditions; the standing associations are therefore confounded by location by
construction, and are treated as descriptive.

---

## 4. Dashboard variable reference

The interactive dashboard crosses one environmental measure against one LIHTC measure
on a three-by-three bivariate grid, at the selected geography. The variables it
exposes, their construction, and their reading:

| Dashboard variable | Field | Definition and computation | Units | Reading and caveats |
|---|---|---|---|---|
| Tree canopy % (all) | `canopy_pct` | Sum of canopy classes 3, 10, 11, 12 as a share of classified land area (water and nodata excluded), from the 1 m Chesapeake land cover | percent | Primary greenness measure. Nominal 2017 (DC) / 2018 (MD). Missing for units flagged `canopy_reliable = False`. |
| Tree canopy % (natural only) | `natural_canopy_pct` | Class 3 only, over classified land | percent | Sensitivity alternative. Excludes canopy over impervious surfaces, lowering values most in built-up areas. |
| Summer surface temp (°C) | `mean_lst` | Area-weighted mean of the three-summer (2021–2023, Jun–Aug) median LST | °C | Daytime land-surface (skin) temperature, not air temperature. Effective resolution ~100 m. |
| NDVI (vegetation) | `mean_ndvi` | Area-weighted mean of the median NDVI | index, −1 to 1 | Greenness independent of the land-cover classification; correlated with canopy. |
| NDBI (built-up) | `mean_ndbi` | Area-weighted mean of the median NDBI | index, −1 to 1 | Built-up / impervious signal; a spectral complement to the land-cover impervious classes. |
| LIHTC low-income units | `lihtc_units_low_income` | Sum of imputed low-income units (`li_unitr`) of projects in the unit | count | Standing stock, not new construction. Zero for units with no project. |
| LIHTC total units | `lihtc_units_total` | Sum of imputed total units (`n_unitsr`) | count | Includes market-rate units in mixed-income LIHTC projects. |
| LIHTC project count | `lihtc_projects` | Number of projects assigned to the unit | count | Insensitive to project size. |
| Clear observations | `mean_clear_obs` | Mean clear-observation count feeding the LST/index medians | count | Reliability companion for the Landsat measures; low values signal a less certain median. |
| Canopy reliability | `canopy_reliable` | Flag; false where classified land falls short of Census land area | boolean | Screens open-water and military-nodata units out of canopy views. |

The dashboard's classification defaults to natural breaks (Jenks). The LIHTC measures
are strongly zero-inflated, and quantile breaks on a variable where most units are
zero place both cut points at zero, which collapses the LIHTC axis to two classes and
empties the high-LIHTC cell. Natural breaks avoids this; the tool warns when a chosen
scheme degenerates.

The bivariate map's corner cell marks co-location: the units where the most LIHTC
coincides with the worst environmental value (lowest canopy, or highest temperature).
The corner is defined by each variable's polarity and moves accordingly. It is a
descriptive display of overlap. Absent a comparison housing group it does not measure
disadvantage, and the dashboard states this.

---

## 5. Reproducibility

The environmental composite is produced by an Earth Engine script (JavaScript for the
Code Editor, with a Python port using `earthengine-api`) that records its parameters
and writes a provenance manifest with each export. The zonal aggregation, LIHTC
processing, and unit construction run in a cached Python pipeline (`greengap/`), so
each step is reproducible and re-runs only when its inputs change. Both census
geographies are built from the same code path. The derived analysis tables and the
dashboard's data are versioned. Exact class definitions, scale factors, masking bits,
and coefficients are as stated above and are set as named constants in the code rather
than inline.

---

## 6. Cross-cutting considerations and limitations

**Temporal alignment.** The three data streams are not contemporaneous. Canopy is
nominal 2017 (DC) / 2018 (MD), surface temperature is a 2021–2023 summer climatology,
and the LIHTC inventory is current. Canopy and the built environment change slowly at
the block-group scale, which limits the practical effect, but the misalignment is a
genuine source of measurement error and constrains any claim that ties a specific
temperature to a specific canopy or housing configuration at a point in time.

**Modifiable areal unit problem.** Every measure is an areal aggregate, and the choice
between block group and tract changes both the smoothing and, potentially, the
associations. The block-group / tract pair is carried precisely to expose this. The
canopy–temperature relationship is stable across the two scales (rank correlation
−0.80 at tract, −0.79 at block group), which is reassuring for the environmental
measures; the LIHTC–temperature association is weaker at the finer scale (rank
correlation +0.28 at tract, +0.20 at block group), the attenuation expected under
aggregation, and neither figure is adjusted for any covariate.

**Ecological aggregation.** A unit's environmental mean is not the exposure at the
housing site. A parcel- or building-level measure with a defined catchment is the
appropriate refinement and is planned; until then, exposure is approximated by the
containing unit.

**Surface versus experienced heat.** LST is a daytime surface measure and stands in
for, rather than equals, the heat residents experience. Air temperature, nighttime
conditions, humidity, and indoor environment are outside the current data.

**No comparison group and no adjustment.** The present analysis contains LIHTC alone
and no covariates. Reported relationships are unconditional associations across areal
units. LIHTC location tracks urban location, and urban land is hot and less green, so
the associations partly restate that geography. They are descriptive, they motivate
the question, and they do not establish a disparity or a mechanism. The estimand the
design ultimately targets, a difference in environmental exposure across housing
types after conditioning on parcel, neighborhood, and jurisdiction characteristics,
requires a market-rate (and other-assisted) comparison group and a set of controls
that are not yet assembled.

---

## References

Chesapeake Bay Program (2023). *Chesapeake Bay Land Use and Land Cover Database, 2022
Edition*. U.S. Geological Survey data release. https://doi.org/10.5066/P981GV1L

Liang, S. (2001). Narrowband to broadband conversions of land surface albedo: I.
Algorithms. *Remote Sensing of Environment*, 76(2), 213–238.

U.S. Census Bureau (2022). *TIGER/Line Shapefiles* (block groups, tracts, counties).

U.S. Department of Housing and Urban Development. *Low-Income Housing Tax Credit
(LIHTC) Database*. https://lihtc.huduser.gov

U.S. Geological Survey. *Landsat Collection 2 Level-2 Science Products*.
https://www.usgs.gov/landsat-missions/landsat-collection-2-level-2-science-products

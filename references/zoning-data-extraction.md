# Zoning Data Acquisition Plan: Maryland + District of Columbia

Green Gap Study, seven-jurisdiction corridor. Supports covariate **C2 (land-use / zoning category)**.
All sources public and free. Verified July 2026.

---

## 0. The structural difference that shapes everything

DC and Maryland are opposite cases, and this determines the whole approach.

- **DC** has a single zoning authority (the Office of Zoning) and one unified regulation, ZR16. One clean, daily-updated layer covers the entire District.
- **Maryland zoning is local by law.** Under the Maryland Constitution (Art. XI-E municipal home rule, XI-F code counties), each county and each incorporated municipality with planning authority writes and maps its own zoning. There is no single statewide zoning *ordinance*, and within a county the incorporated municipalities zone independently. Montgomery is the textbook case: Rockville and Gaithersburg maintain their own zoning, separate from the county, and are set to merge into one new municipality on 1 July 2026.

This fragmentation is exactly why the covariate framework (comment C13) originally chose a land-use proxy instead of assembling zoning. **That decision can now be upgraded.** The Maryland Department of Planning has already done the assembly: its **Statewide Generalized Zoning** dataset collects zoning from every county and every municipality with planning authority into one harmonized layer. So you can use actual zoning, harmonized, rather than substituting land use for it.

The plan therefore has two tiers for Maryland. Use Tier 1 as the operational corridor-wide variable; reach for Tier 2 only where you need finer categories than "generalized" provides.

---

## 1. District of Columbia

One authority, three complementary layers, all on Open Data DC / DC GIS, updated daily, available as shapefile, GeoJSON, and ArcGIS REST.

| Layer | What it gives you | Link |
|---|---|---|
| **Zoning Boundaries (ZR16)** | Zone polygons under the 2016 regulations (the geometry) | https://catalog.data.gov/dataset/zoning-boundaries-zoning-regulations-of-2016 |
| **Zoning Development Standards** | Allowable density, height, FAR, lot occupancy by zone (the *intensity* your covariate needs) | https://catalog.data.gov/dataset/zoning-development-standards |
| Zoning search / hub | Discovery across all zoning datasets | https://opendata.dc.gov/search?categories=zoning · https://datahub-dc-dcgis.hub.arcgis.com/search?categories=zoning |
| Interactive ZR16 map (visual check) | https://maps.dcoz.dc.gov/zr16/ |

ZR16 superseded the 1958 regulations on 6 September 2016. Zone codes are the ZR16 scheme (for example R, RF, RA residential; MU mixed-use; PDR production/distribution/repair). Join **Zoning Boundaries to Zoning Development Standards on the zone code** to convert a zone label into a numeric intensity.

---

## 2. Maryland

### Tier 1 (recommended): MDP Statewide Generalized Zoning

One dataset, whole state, already harmonized across counties and municipalities.

| What | Link |
|---|---|
| **Generalized Zoning 2025** (REST) | https://mdpgis.mdp.state.md.us/arcgis/rest/services/PlanningCadastre/Generalized_Zoning_2025/MapServer/0 |
| Generalized Zoning current (REST) | https://mdpgis.mdp.state.md.us/arcgis/rest/services/PlanningCadastre/Generalized_Zoning/MapServer/0 |
| MDP iMaps (product page) | https://planning.maryland.gov/Pages/OurProducts/iMaps.aspx |
| MD iMAP Data Catalog (search "zoning") | https://data.imap.maryland.gov/ |

- Editions: 2010, 2021, 2025. The 2025 edition folds in municipal annexations reviewed through September 2025.
- MDP collapses hundreds of local zones into **Generalized Zoning Categories** (three families: Rural, Residential, Other) with an allowable-density range attached, based on each jurisdiction's zone intent and density.
- Because it is collected from the localities, it already includes the incorporated municipalities that the county layers in Tier 2 may omit. This is its decisive advantage for a corridor-wide, consistent variable.
- Cost of the harmonization: it is *generalized*. If your covariate needs to separate, say, R-60 from R-90 single-family density tiers, the generalized layer will not resolve that; go to Tier 2 for those jurisdictions.

### Tier 2 (optional): per-jurisdiction detailed zoning

Use only where you need finer categories than Tier 1. Each is free; formats and portals differ.

| Jurisdiction | Authority | Portal / notes |
|---|---|---|
| Baltimore City | City Planning | https://planning.baltimorecity.gov/maps-data/gis · map gallery https://mapgallery-baltplanning.hub.arcgis.com/ · OpenBaltimore |
| Baltimore County | County OIT/GIS | https://opendata.baltimorecountymd.gov/ · download https://www.baltimorecountymd.gov/departments/information-technology/gis/data-download (shapefile / GDB / FTP) |
| Montgomery County | M-NCPPC (Montgomery Planning) | Zoning geodatabase: https://data-mcplanning.hub.arcgis.com/datasets/ac40004eb81845ae8634647741a976d4 · hub https://opendata-mcgov-gis.hub.arcgis.com/ · **Rockville and Gaithersburg maintained separately** (see note): https://montgomeryplanning.org/tools/gis-and-mapping/zoning-map-maintenance/ |
| Prince George's County | M-NCPPC (PG Planning) | GIS Open Data Portal https://gisdata.pgplanning.org/opendata/ · metadata https://gisdata.pgplanning.org/metadata/ · zoning map service https://gisdata.pgplanning.org/arcgis/rest/services/Map_Services/C_I_Z/MapServer (shapefile / GDB / DXF) |
| Anne Arundel County | County OPZ | OpenArundel https://opendata.aacounty.org/ · **City of Annapolis zones separately**: https://www.annapolis.gov/246/GIS-Data-Downloads |
| Howard County | Planning & Zoning | Zoning download https://data.howardcountymd.gov/datadownload/metadata/zoning.html · portal https://data.howardcountymd.gov/ |

**The municipal-fragmentation caveat.** County zoning layers in Tier 2 often cover only the unincorporated county and exclude incorporated municipalities that zone themselves (Rockville and Gaithersburg in Montgomery, Annapolis in Anne Arundel, and others). If you go Tier 2, you must pull each relevant municipality separately and mosaic them, or you will have holes exactly where those cities sit. Tier 1 does not have this problem, which is the main reason to prefer it.

---

## 3. Harmonizing to a common typology

Even Tier 1 (MD Generalized categories) and DC (ZR16 codes) are different schemes, so a crosswalk is required before the variable can be used corridor-wide. The covariate's operative logic is "higher-intensity zoning carries lower canopy," so map every source zone to a single **ordinal intensity typology**, for example:

1. Open space / institutional
2. Rural / very low density residential
3. Low-density residential
4. Medium-density residential
5. High-density / multifamily residential
6. Mixed-use
7. Commercial
8. Industrial / PDR

Build this as an explicit lookup table: source zone code → common class. Two rows of provenance, MD and DC, per common class. This table is an appendix deliverable, not scratch work, and a second reader should check the multifamily and mixed-use rows, since those are where your treatment concentrates.

---

## 4. Processing and joining to parcels

Zoning is a **polygon** layer with no shared ID to the parcel table, so the link is spatial, not a key join.

1. Reproject every zoning layer to the common CRS used for the parcels (NAD83 Maryland State Plane, EPSG:26985, covers the whole corridor including DC).
2. Assign each parcel a zone by **point-in-polygon on the parcel centroid**. Fast and unambiguous for most parcels.
3. Detect **split-zoned parcels** (a parcel intersecting more than one zone) with an areal overlay; flag them and assign the majority-area zone, keeping the split flag as a covariate-robustness variable. Large multifamily and redevelopment parcels are disproportionately split-zoned, so do not skip this.
4. Attach the common intensity class via the crosswalk.
5. For DC, join the development-standards density/FAR onto the zone so you have a continuous intensity measure, not only a category.

### QA checks

- Every parcel receives a zone; count and inspect the unmatched (usually rights-of-way, water, federal land).
- Municipal coverage present (spot-check a Rockville, a Gaithersburg, an Annapolis parcel).
- Split-zoned share is plausible, not near zero (near zero means the overlay failed).
- Vintage recorded per source; MDP is dated by edition, DC is a daily snapshot, county layers vary.

---

## 5. Recommendation and effect on the framework

Use **MDP Statewide Generalized Zoning (2025) for the six Maryland jurisdictions and DC ZR16 Zoning Boundaries plus Development Standards for the District**, harmonized to the common intensity typology. Hold Tier 2 in reserve for a robustness check in the two or three jurisdictions where generalized categories prove too coarse.

This upgrades covariate **C2**. The framework currently substitutes MDP *land use / land cover* for zoning because zoning looked too fragmented to assemble. With the Generalized Zoning layer, you can use *actual zoning* (allowable intensity), harmonized and free, which is a more direct measure of the "zoning governs allowable density, setbacks, and landscaping" mechanism the covariate is meant to capture. Land use and zoning are different things (what exists versus what is permitted); you may keep both, land use as realized cover and zoning as regulatory intent.

---

## 6. Pitfalls

- **Land use is not zoning.** Do not treat the MDP Land Use/Land Cover layer and the MDP Generalized Zoning layer as interchangeable; they answer different questions.
- **County layers omit municipalities.** The single most likely coverage hole in Tier 2. Tier 1 avoids it.
- **Split-zoned parcels** bias any naive centroid-only assignment on exactly the large parcels you care about.
- **Rockville–Gaithersburg merger (July 2026)** will change municipal geometry mid-study; pin your zoning vintage and note it.
- **Overlay zones** (historic, TDR, environmental) sit on top of base zoning; decide whether they matter for canopy and handle them explicitly rather than letting them silently replace the base zone.
- **Cross-jurisdiction code collisions.** "R-60" or "C-2" mean different things in different jurisdictions; never compare raw zone codes across the corridor, only the harmonized class.
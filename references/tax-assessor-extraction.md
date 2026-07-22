ssessor Data Extraction and Processing Plan: Maryland + District of Columbia

Green Gap Study, seven-jurisdiction corridor (Baltimore City; Baltimore, Prince George's, Montgomery, Anne Arundel, Howard Counties; District of Columbia). All sources below are public and free. Verified July 2026.

0. Architecture in one paragraph

Two jurisdictions, two assessment systems, two primary keys. Maryland keys on ACCTID (parcel account number); DC keys on SSL (Square, Suffix, Lot). Both publish Computer Assisted Mass Appraisal (CAMA) data, parcel geometry, and sales, as bulk downloads and through APIs. The end product is one harmonised parcel table with a common parcel_uid, carrying assessed values, building and land characteristics, and geometry, to which land cover, LST, and housing-type labels are then joined.

1. Maryland
1.1 Sources
What	Link
Bulk downloads hub (CAMA, sales, parcels, land use)	https://planning.maryland.gov/pages/ourproducts/downloadfiles.aspx
Parcel data background	https://planning.maryland.gov/MSDC/Pages/91_property_mapping/parcel-data.aspx
MD iMAP GIS Data Catalog	https://data.imap.maryland.gov/
CAMA Core (catalog entry)	https://data.imap.maryland.gov/datasets/maryland::maryland-computer-assisted-mass-appraisal-cama-core/about
CAMA Land (Socrata, API)	https://opendata.maryland.gov/dataset/CAMA-Land/yu7q-jvwy
CAMA Detailed Building Characteristics (Socrata, API)	https://opendata.maryland.gov/dataset/CAMA-Detailed-Building-Characteristics/qjbk-5mtj
Property Data REST (parcel points)	https://geodata.md.gov/imap/rest/services/PlanningCadastre/MD_PropertyData/MapServer/layers
Parcel Boundaries REST	https://geodata.md.gov/imap/rest/services/PlanningCadastre/MD_ParcelBoundaries/MapServer/0
Property Sales REST	https://mdpgis.mdp.state.md.us/arcgis/rest/services/PlanningCadastre/Property_Sales/MapServer
FINDER Quantum (free desktop GIS bundle)	https://planning.maryland.gov/Pages/OurProducts/PropertyMapProducts/FinderProduct.aspx

From the bulk hub, take: Statewide CAMA (2026 Q1; 2025 Q1–Q4; 2024 Q2 split into Core/Building/Land/Subareas; 2020; 2017), Property Sales (monthly, plus PropertySales_2026_Schema.xlsx), MdProperty View parcel points and polygons per county or the February 2026 statewide geodatabase, plus MdPropertyView_2026_Schema.xlsx, and Land Use 2018 (2024 Edition).

1.2 Structure and keys
ACCTID = parcel account number, the primary key.
CAMA Core is the hub. Building, Building Subareas, and Land all join to Core.
Building → Core on ACCTID
Building → Building Subareas on CAMALINK (multi-record; expect many rows per parcel)
Vintage fields: sdatdate (date of most recent SDAT assessment linkage) and mdpvdate (MdProperty View publication date). Use these for the H2 data-vintage alignment, not the file name.
Source of record is SDAT, with MDP-added fields.
1.3 Steps
Download the CAMA quarter matching your land-cover vintage, plus the parcel file for the same period.
Read both schema workbooks before writing any parsing code; field names differ across editions.
Subset to the six Maryland jurisdictions by county code.
Join Core → Building → Land on ACCTID; join Subareas on CAMALINK and aggregate to one row per ACCTID before merging.
Attach parcel geometry (points for centroid work, polygons for area and buffers) on ACCTID.
Filter to multifamily rental using the SDAT land-use / structure codes. Identify the exact codes from the schema, do not assume them.
Attach Property Sales on ACCTID for the transaction features.
2. District of Columbia
2.1 Sources
What	Link
Open Data DC hub	https://opendata.dc.gov/
CAMA Residential	https://opendata.dc.gov/datasets/DCGIS::computer-assisted-mass-appraisal-residential/about
CAMA Commercial (multifamily lives here)	https://opendata.dc.gov/datasets/computer-assisted-mass-appraisal-commercial/explore
CAMA Condominium	https://opendata.dc.gov/datasets/computer-assisted-mass-appraisal-condominium/explore
Tax System Property Sales (CAMA)	https://opendata.dc.gov/maps/DCGIS::tax-system-property-sales-cama
Parcel Lots (geometry)	https://opendata.dc.gov/datasets/DCGIS::parcel-lots/about
Address ↔ SSL Cross Reference	https://opendata.dc.gov/datasets/DCGIS::address-and-square-suffix-lot-cross-reference/about
Existing Land Use	https://opendata.dc.gov/datasets/DCGIS::existing-land-use/about
Parcel/lot metadata	https://dcatlas.dcgis.dc.gov/metadata/ParcelsLotsPly.html
Tax lot metadata	https://dcatlas.dcgis.dc.gov/metadata/TaxLotsPly.html
OTR Real Property GIS programme	https://otr.cfo.dc.gov/page/real-property-geographic-information-systems-gis-program
Real Property Public Extract (restricted, not a research channel)	https://otr.cfo.dc.gov/page/real-property-public-extract-records
2.2 Structure and keys
SSL (Square, Suffix, Lot) = primary key. DC's own documentation: "Data will be most useful when joined by SSL to other real property data."
Suffixes are directional (N/S/E/W) and most squares have none. Preserve SSL as a string; never cast to numeric.
CAMA is split into three files by property type. Multifamily rental sits mainly in Commercial, not Residential. Pulling only Residential silently drops most of the DC rental stock.
Condominium file carries one record per unit; aggregate to building before comparing with Maryland parcels.
Updated daily, and each extract is a snapshot. Record the extraction timestamp.
2.3 Steps
Pull CAMA Commercial first, then Residential and Condominium.
Pull Parcel Lots for geometry; join on SSL.
Pull Tax System Property Sales; join on SSL.
Use the Address ↔ SSL cross reference to attach street addresses, which is also your bridge for matching CoStar records.
Filter to multifamily use codes; verify codes against the metadata pages rather than assuming.
3. Harmonisation

The two systems do not share a key, a schema, or use codes. Build an explicit crosswalk rather than coercing one into the other.

Common identifier: create parcel_uid = "MD:" + ACCTID or "DC:" + SSL. Keep the native key in its own column.
Field mapping table: one row per target variable (assessed_total, assessed_land, assessed_improvement, year_built, units, building_area, lot_area, use_code, sale_price, sale_date), with the MD source field, the DC source field, and any unit conversion. This table is a deliverable, not scratch work; it goes in the appendix.
Use-code harmonisation is the hard part. SDAT land-use codes and DC use codes are unrelated schemes. Hand-build the multifamily crosswalk and have a second person check it.
Vintage: Maryland is quarterly with sdatdate; DC is a daily snapshot. Choose one as-of date, pull the nearest Maryland quarter, and record both.
CRS: reproject everything to a single projected CRS before any area or distance work. NAD83 Maryland State Plane (EPSG:26985) covers the whole corridor including DC.
4. Pipeline

Suggested stack: Python with geopandas for geometry, duckdb for the large joins (statewide CAMA is big), pyarrow/Parquet for intermediates.

Ingest raw files unchanged to data/raw/, one folder per source per download date. Never edit in place.
Standardise to Parquet with typed columns; SSL and ACCTID as strings.
Join within jurisdiction (MD: Core+Building+Land+Subareas; DC: CAMA+Parcels+Sales).
Filter to multifamily rental.
Harmonise to the common schema via the mapping table.
Union MD + DC into one parcel table.
Spatial join to 1 m land cover, LST, FEMA zone, transit distance.
Label housing type by joining NHPD, HUD LIHTC, HUD Picture, then the rent threshold.
QA checks (run every refresh)
Row counts by jurisdiction against the published record counts.
Duplicate parcel_uid count must be zero after aggregation.
Null rate per field, by jurisdiction, flagged when it differs sharply between MD and DC.
Assessed total ≈ land + improvement, within rounding.
Geometry validity and CRS check; parcels with zero or absurd area.
Year built outside plausible range.
Unmatched records retained in an explicit file, never silently dropped.
5. Known pitfalls
DC Residential-only pull drops most multifamily. The single most likely silent failure.
Maryland CAMA is split across four themes; using Core alone loses building detail.
DC condominium records are per unit, so building-level aggregation is required for comparability.
Multi-parcel properties: one apartment complex can span several ACCTIDs or SSLs. Decide the aggregation rule and apply it consistently.
Baltimore City ground rents create leasehold/fee-simple splits that can duplicate or fragment records.
Tax-exempt parcels may carry zero or missing assessed values; exclude deliberately rather than letting zeros enter the value features.
Assessment cycles differ; Maryland reassesses on a three-year cycle by group, so assessed values are not all struck in the same year.
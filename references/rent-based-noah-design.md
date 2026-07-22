# Rent-Based NOAH: Design Memo

Green Gap study. A design for defining NOAH from **observed rent per unit** rather
than from assessed value per unit. Written to be reviewed before any code is built.

## 1. Why rent, not value

NOAH is a supply-side rent phenomenon. A unit is NOAH when the rent the local market
sets for it is affordable — not when the property's assessed value is low. Value and
rent diverge exactly where the label is decided:

- Two buildings with the same assessed value per unit can charge very different rents
  (submarket, amenities, landlord strategy, regulation, unit mix).
- Assessment lags the market and is struck on multi-year cycles; rent is set now.
- Value capitalizes expected future rent plus land, so it loads on location and
  appreciation, not current affordability.

The AMI-anchored value cutoff we built (`greengap.noah_threshold`) is an improvement
over an arbitrary quantile, but it is still a proxy: it asks "is this building worth
what an affordable building is worth", not "does this building rent affordably". The
rent-based definition asks the right question directly.

## 2. The affordability test, in rent terms

A unit is NOAH if its rent is affordable at a target AMI level (60% AMI default, the
LIHTC standard), under the 30%-of-income rule:

    affordable rent (monthly) = 0.30 × (AMI_level income, size-adjusted) / 12

Size-adjust by bedroom count using HUD's convention (household size = bedrooms + 1.5,
rounded), so a 2-bedroom unit is tested against a ~3-person income limit, a 1-bedroom
against ~2-person, etc. This replaces the value chain (rent → GRM → assessment ratio)
entirely: the comparison is rent to rent.

NOAH_unit  = observed_rent ≤ affordable_rent(bedrooms, AMI_level)

Aggregate to the building: a building is NOAH if a majority (or a chosen share) of its
observed units rent at or below the affordable line. The aggregation rule is a
documented choice, tested for sensitivity like the AMI level.

## 3. Data: Dewey RentHub

Source is **Dewey RentHub** (see `references/Dewey-Data-Dict.xlsx`), not CoStar.
RentHub is listing-level rental panel data licensed through Dewey, so it carries no
CoStar proprietary restriction.

| Property | RentHub |
|---|---|
| Unit of analysis | individual advertised rental unit (listing) |
| Rent field | **asking rent** (+ price per sqft) |
| Unit detail | bedrooms, property type, amenities, building age |
| Location | exact address + lat/lon when available |
| Coverage | nationwide, 2014+, weekly snapshots |

**Not yet on disk.** Only the dictionary is present; the RentHub extract for the
corridor still has to be obtained.

## 4. The join

The parcels carry geometry but not a clean *property* street address (the `owner`
field is a mailing address). So the bridge is **spatial**, not string-matched:

1. Geocode each RentHub listing to a point (lat/lon in the extract, or geocode the
   address).
2. Spatial-join listings to the multifamily building footprints
   (`mf_buildings*.parquet`), point-in-polygon, within a small snap tolerance for
   listings geocoded to the street centroid.
3. Collapse listings to the building: one rent distribution per building, keyed on
   `building_id`, with bedroom mix preserved.

A listing that falls in no multifamily building is dropped to an explicit unmatched
file (single-family rentals, condos, listings outside the corridor). The match rate
is a headline diagnostic.

## 5. What this fixes, and what it does not

Fixes:

- NOAH becomes a **rent** label, set by observed local market rents at the building.
- The affordability line is rent-to-rent, no GRM or assessment-ratio assumptions.
- Bedroom-size adjustment makes the test unit-appropriate.

Does not fix — and must be stated:

- **Asking, not contract rent.** RentHub observes advertised rents, which run above
  executed lease rents, and above the rents sitting tenants pay. A building can rent
  affordably to its current tenants yet advertise above the line. This biases toward
  under-counting NOAH.
- **Advertising coverage bias.** Coverage is strongest where rentals are actively
  advertised — professionally-managed and larger buildings. The small, informally-
  rented NOAH stock (the exact stock most at risk of loss) is systematically thinner
  in the data. This is the same bias CoStar has, and it cuts against the study's core
  population. It must be quantified (match rate by building size) and flagged.
- **Coverage is not universal.** Buildings with no listings in the window get no rent
  observation. They cannot be labeled from rent and fall to `unknown`, or need a
  fallback (see §6).
- **Selection into listing.** A vacant advertised unit is not a random draw from the
  building; turnover correlates with rent changes.

## 6. Handling incomplete coverage

Two options, to decide:

- **Rent where available, value elsewhere.** Buildings with enough listings are
  labeled from rent; the rest fall back to the AMI-anchored value cutoff, with a flag
  recording which method labeled each building. Maximizes coverage, mixes two
  definitions.
- **Rent-only, restricted sample.** Label only buildings with rent observations; the
  analysis runs on that subsample, with the coverage bias characterized. Cleaner
  definition, smaller and non-random sample.

Recommendation: build **both** the rent label (primary) and keep the value label
(fallback + comparison), report how often they agree, and treat the value cutoff as a
robustness anchor rather than discard it. Disagreement between the two is itself a
finding about where value and rent diverge.

## 7. Build sequence (once RentHub is in hand)

1. Ingest the RentHub corridor extract to `data/raw/`, unchanged.
2. Geocode + spatial-join listings to `mf_buildings`; write per-building rent
   distributions (median rent, rent/bedroom, n listings, bedroom mix).
3. Compute the bedroom-size-adjusted affordable rent per AMI level (HUD income
   limits already pinned in `greengap.noah_threshold`).
4. Label buildings NOAH/market from rent; carry the value label alongside.
5. Rebuild the analysis frame, regression, and dashboard on the rent label; add a
   rent-vs-value agreement panel.
6. QA: match rate overall and by building size; asking-vs-value rent gap; share of
   buildings labeled by each method; unmatched listings retained.

## 8. Open decisions for the researcher

- **AMI level** for the rent test — 60% default is consistent with the value cutoff;
  keep 50/80 as variants.
- **Building aggregation rule** — majority of units affordable, or a threshold share.
- **Coverage handling** — rent-only vs rent-with-value-fallback (§6).
- **Bedroom adjustment** — HUD (bedrooms + 1.5) vs a fixed reference household.
- **Asking-rent correction** — leave as asking, or discount toward contract rent
  using a published asking-to-contract ratio (adds an assumption).

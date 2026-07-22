"""AMI-anchored NOAH cutoff: an affordability threshold on assessed value per unit.

Replaces the earlier per-state quantile with a cutoff tied to an explicit
affordability standard. The chain, each step sourced or measured:

    affordable rent  = 30% of the HUD FY2024 80%-AMI income limit (3-person
                       reference household, a 2-bedroom unit) / 12          [HUD]
    affordable
    market value/unit = affordable annual rent x gross rent multiplier (GRM) [stated]
    affordable
    assessed value    = affordable market value x assessment ratio           [measured]
      /unit             = the NOAH cutoff

A building is NOAH if its assessed value per unit is at or below this cutoff and it
is unsubsidized; market-rate if above. The cutoff is per state because the AMI limit
and the assessment ratio both differ by state.

Why assessed, not market, value
--------------------------------
The parcels carry *assessed* value, which in MD and DC sits below market. Applying a
market GRM to affordable rent yields an affordable *market* value; multiplying by the
**assessment ratio** brings it onto the same footing as the assessed values the
buildings actually carry. The assessment ratio is not assumed - it is estimated from
arms-length multifamily sales in the study parcels themselves (assessed / sale
price), so it reflects these exact properties in these exact years.

Constants are pinned and cited rather than fetched at run time: the AMI figures are a
small, stable, auditable set, and a dissertation should not depend on a live HUD
download. ``data/external/HUD_Section8_IncomeLimits_FY24.xlsx`` is the source file.
"""

from __future__ import annotations

import geopandas as gpd
from loguru import logger
import pandas as pd

# HUD FY2024 Section 8 income limits, 80% (Low Income) limit, 3-person household.
# Source: HUD USER, Section8-FY24.xlsx (il24). The six MD study counties share the
# Baltimore-Columbia-Towson and Washington metros; DC + Montgomery + Prince George's
# share the Washington-Arlington-Alexandria metro. Figures verified 2026-07.
AMI80_3PERSON = {
    "District of Columbia": 88_050,
    "Montgomery": 88_050,
    "Prince George's": 88_050,
    "Anne Arundel": 88_000,
    "Baltimore County": 88_000,
    "Baltimore City": 88_000,
    "Howard": 88_000,
}
# State-level fallback (metro-average 80% AMI, 3-person) when a jurisdiction name is
# not matched: keeps the cutoff defined for every building.
AMI80_STATE = {"DC": 88_050, "MD": 88_000}

# NOAH is defined at a target AMI level below 80%. 60% is the default - it is the
# LIHTC targeting standard, so NOAH is drawn at the same income line the subsidized
# (mostly LIHTC) stock serves. 50% and 80% bracket it for sensitivity. HUD's income
# limits are (roughly) linear in the AMI fraction, so lower levels scale the 80%
# limit by (level / 0.80).
AMI_LEVELS = {"ami50": 0.50, "ami60": 0.60, "ami80": 0.80}
DEFAULT_AMI_LEVEL = "ami60"

# Affordability rule and rent->value conversion.
RENT_INCOME_SHARE = 0.30      # 30% of income to gross rent (standard affordability)
GROSS_RENT_MULTIPLIER = 10.0  # market value / annual gross rent; ~9-11x for stabilised
                              # multifamily. Sensitivity variants bracket this.


def ami_income(jurisdiction: str, state: str, level: float) -> float:
    """3-person income limit at ``level`` AMI, scaled from the pinned 80% limit."""
    ami80 = AMI80_3PERSON.get(jurisdiction, AMI80_STATE.get(state, 0))
    return ami80 * (level / 0.80)

# Bounds for a sale to enter the assessment-ratio estimate: a plausible price, a
# recent-enough sale, and a ratio inside a sane band (partial-interest and nominal
# deeds otherwise distort it).
SALE_MIN_PRICE = 10_000
SALE_MIN_YEAR = 2015
RATIO_BAND = (0.05, 3.0)


def estimate_assessment_ratio(parcels: gpd.GeoDataFrame) -> dict[str, float]:
    """Median assessed/sale ratio per state from arms-length multifamily sales.

    Uses the study parcels' own recorded sales, so the ratio reflects these
    properties, not a borrowed statewide figure. The median is robust to the
    partial-interest and stale-sale tails that widen the raw distribution.
    """
    price = pd.to_numeric(parcels["sale_price"], errors="coerce")
    year = pd.to_datetime(parcels["sale_date"], errors="coerce").dt.year
    total = pd.to_numeric(parcels["assessed_total"], errors="coerce")

    ok = (price > SALE_MIN_PRICE) & (total > 0) & (year >= SALE_MIN_YEAR)
    ratio = (total / price).where(ok)
    ratio = ratio.where((ratio > RATIO_BAND[0]) & (ratio < RATIO_BAND[1]))

    out = {}
    for state, idx in parcels.groupby("state").groups.items():
        r = ratio.loc[idx].dropna()
        out[state] = float(r.median()) if len(r) else 1.0
        logger.info(
            f"assessment ratio[{state}]: median={out[state]:.2f} (n={len(r)} sales)"
        )
    return out


def affordable_assessed_value_per_unit(
    jurisdiction: str,
    state: str,
    assessment_ratio: float,
    level: float,
    grm: float = GROSS_RENT_MULTIPLIER,
) -> float:
    """The NOAH cutoff for one jurisdiction: affordable assessed value per unit."""
    affordable_annual_rent = ami_income(jurisdiction, state, level) * RENT_INCOME_SHARE
    affordable_market_value = affordable_annual_rent * grm
    return affordable_market_value * assessment_ratio


def cutoff_map(
    parcels: gpd.GeoDataFrame, level: float, grm: float = GROSS_RENT_MULTIPLIER
) -> dict[str, float]:
    """{jurisdiction -> assessed value/unit cutoff} at one AMI level."""
    ratios = estimate_assessment_ratio(parcels)
    out = {}
    for juris in AMI80_3PERSON:
        state = "DC" if juris == "District of Columbia" else "MD"
        out[juris] = affordable_assessed_value_per_unit(
            juris, state, ratios.get(state, 1.0), level, grm
        )
    return out


def noah_cutoffs(
    parcels: gpd.GeoDataFrame, level: float = AMI_LEVELS[DEFAULT_AMI_LEVEL],
    grm: float = GROSS_RENT_MULTIPLIER,
) -> pd.DataFrame:
    """Per-jurisdiction NOAH cutoffs with the intermediate quantities exposed."""
    ratios = estimate_assessment_ratio(parcels)
    rows = []
    for juris in AMI80_3PERSON:
        state = "DC" if juris == "District of Columbia" else "MD"
        ar = ratios.get(state, 1.0)
        income = ami_income(juris, state, level)
        rows.append({
            "jurisdiction": juris,
            "state": state,
            "ami_level": level,
            "income_3person": round(income),
            "affordable_monthly_rent": round(income * RENT_INCOME_SHARE / 12),
            "assessment_ratio": round(ar, 3),
            "grm": grm,
            "cutoff_value_per_unit": round(
                affordable_assessed_value_per_unit(juris, state, ar, level, grm)
            ),
        })
    return pd.DataFrame(rows)

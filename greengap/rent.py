"""ACS rent measures and a rent-based NOAH signal.

The value-based NOAH cutoff (``greengap.noah_threshold``) infers affordability from
assessed value per unit. NOAH is really a *rent* concept, so this module adds
observed rent from the Census to (1) give a rent-denominated affordability signal
and (2) validate the value proxy. Three ACS 2022 5-year tables:

    B25064  median gross rent            block group   overall level
    B25063  gross rent distribution      block group   bracket counts -> affordable share
    B25031  median gross rent by bedroom  tract         bedroom-size detail (BG has none)

Rent is a *neighborhood* measure here (block group / tract), not a building rent:
ACS does not publish rent per property. So the rent-based NOAH signal is the share
of a block group's renter units that rent affordably - a contextual affordability
measure, stronger than assessed value but still not the building's own rent. The
building-level rent still needs listing data (see references/rent-based-noah-design.md).

Requires ``CENSUS_API_KEY`` (.env).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import urlopen

from loguru import logger
import numpy as np
import pandas as pd
import typer

from greengap.acs import ACS_BASE, STATE_COUNTIES
from greengap.config import INTERIM_DATA_DIR

app = typer.Typer(help=__doc__, no_args_is_help=True)

# Block-group tables: median rent + the full gross-rent bracket distribution.
BG_MEDIAN_VAR = "B25064_001E"
# B25063 cash-rent brackets, in order, with each bracket's UPPER bound (dollars).
# The last open bracket ($3,500+) has no finite upper bound. Used to compute the
# share of renter units at or below an affordability line by interpolation.
RENT_BRACKETS = [
    ("B25063_003E", 100), ("B25063_004E", 150), ("B25063_005E", 200),
    ("B25063_006E", 250), ("B25063_007E", 300), ("B25063_008E", 350),
    ("B25063_009E", 400), ("B25063_010E", 450), ("B25063_011E", 500),
    ("B25063_012E", 550), ("B25063_013E", 600), ("B25063_014E", 650),
    ("B25063_015E", 700), ("B25063_016E", 750), ("B25063_017E", 800),
    ("B25063_018E", 900), ("B25063_019E", 1000), ("B25063_020E", 1250),
    ("B25063_021E", 1500), ("B25063_022E", 2000), ("B25063_023E", 2500),
    ("B25063_024E", 3000), ("B25063_025E", 3500), ("B25063_026E", np.inf),
]
BG_RENTER_DENOM = "B25063_002E"  # renter units paying cash rent

# Tract table: median gross rent by bedroom count (BG does not carry it).
TRACT_BEDROOM_VARS = {
    "B25031_002E": "rent_0br", "B25031_003E": "rent_1br", "B25031_004E": "rent_2br",
    "B25031_005E": "rent_3br", "B25031_006E": "rent_4br",
}


def rent_bg_path() -> Path:
    return INTERIM_DATA_DIR / "rent_bg.parquet"


def rent_tract_path() -> Path:
    return INTERIM_DATA_DIR / "rent_tract.parquet"


def _fetch(level: str, get_vars: list[str], key: str) -> pd.DataFrame:
    """Pull ``get_vars`` at block-group or tract level for the study counties."""
    frames = []
    for state, counties in STATE_COUNTIES.items():
        for county in counties:
            if level == "bg":
                geo = f"for=block%20group:*&in=state:{state}&in=county:{county}&in=tract:*"
            else:
                geo = f"for=tract:*&in=state:{state}&in=county:{county}"
            url = f"{ACS_BASE}?get={','.join(get_vars)}&{geo}&key={key}"
            rows = json.load(urlopen(url, timeout=120))
            df = pd.DataFrame(rows[1:], columns=rows[0])
            frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    if level == "bg":
        df["GEOID"] = df["state"] + df["county"] + df["tract"] + df["block group"]
    else:
        df["tract_geoid"] = df["state"] + df["county"] + df["tract"]
    return df


def affordable_share(df: pd.DataFrame, affordable_rent: float) -> pd.Series:
    """Share of renter units paying at or below ``affordable_rent`` per month.

    Sums whole brackets below the line and linearly interpolates the bracket the
    line falls inside (renters are assumed uniform within a bracket - the standard
    treatment for grouped rent data). Denominator is cash-rent renter units.
    """
    denom = pd.to_numeric(df[BG_RENTER_DENOM], errors="coerce")
    below = pd.Series(0.0, index=df.index)
    prev_upper = 0.0
    for var, upper in RENT_BRACKETS:
        count = pd.to_numeric(df[var], errors="coerce").fillna(0)
        if upper <= affordable_rent:
            below += count
        elif prev_upper < affordable_rent < upper and np.isfinite(upper):
            frac = (affordable_rent - prev_upper) / (upper - prev_upper)
            below += count * frac
        prev_upper = upper if np.isfinite(upper) else prev_upper
    return (below / denom.replace(0, np.nan)) * 100


def fetch_rent(affordable_rent: float, force: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pull + cache BG rent (median, affordable share) and tract rent-by-bedroom."""
    bg_path, tr_path = rent_bg_path(), rent_tract_path()
    if bg_path.exists() and tr_path.exists() and not force:
        logger.info(f"rent: cached -> {bg_path.name}, {tr_path.name}")
        return pd.read_parquet(bg_path), pd.read_parquet(tr_path)

    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise RuntimeError("CENSUS_API_KEY not set (add it to .env).")

    bg_vars = [BG_MEDIAN_VAR, BG_RENTER_DENOM] + [v for v, _ in RENT_BRACKETS]
    bg = _fetch("bg", bg_vars, key)
    out_bg = pd.DataFrame({"GEOID": bg["GEOID"]})
    out_bg["median_gross_rent"] = pd.to_numeric(bg[BG_MEDIAN_VAR], errors="coerce").where(
        lambda s: s >= 0)  # -666666666 sentinel -> NA
    out_bg["affordable_rent_share"] = affordable_share(bg, affordable_rent)
    out_bg["renter_units"] = pd.to_numeric(bg[BG_RENTER_DENOM], errors="coerce")

    tr = _fetch("tract", list(TRACT_BEDROOM_VARS), key)
    out_tr = pd.DataFrame({"tract_geoid": tr["tract_geoid"]})
    for var, name in TRACT_BEDROOM_VARS.items():
        out_tr[name] = pd.to_numeric(tr[var], errors="coerce").where(lambda s: s >= 0)

    bg_path.parent.mkdir(parents=True, exist_ok=True)
    out_bg.to_parquet(bg_path)
    out_tr.to_parquet(tr_path)
    logger.success(
        f"rent: {len(out_bg):,} BGs (median + affordable share @ ${affordable_rent:,.0f}), "
        f"{len(out_tr):,} tracts (by bedroom) -> {bg_path.name}, {tr_path.name}"
    )
    return out_bg, out_tr


@app.command("build")
def build_cmd(
    affordable_rent: float = typer.Option(
        1650.0, help="Monthly affordable-rent line for the BG affordable share."),
    force: bool = typer.Option(False, help="Re-pull from the Census API."),
):
    """Pull + cache the ACS rent measures."""
    fetch_rent(affordable_rent=affordable_rent, force=force)


if __name__ == "__main__":
    app()

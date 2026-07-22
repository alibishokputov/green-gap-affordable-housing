"""Adjusted type-contrast regression: environmental exposure across housing types.

Estimates the conditional difference in an environmental measure (tree canopy,
summer LST, ...) between NOAH / subsidised multifamily and market-rate multifamily,
after accounting for observed building and neighbourhood characteristics. This is
the estimand the project's design targets: a *conditional* exposure difference
across housing types, not a causal effect.

    outcome    = environmental measure in natural units (pp, degC) - not z-scored,
                 so a coefficient reads directly as "pp of canopy" or "degC"
    key terms  = housing-type dummies, market-rate as the baseline, so each
                 coefficient is that type's gap versus market-rate
    controls   = structure (age, log lot area, log units), neighbourhood ACS
                 (income, poverty, education, race, density), flood zone, and
                 jurisdiction fixed effects
    inference  = heteroskedasticity-robust (HC3) standard errors

Run per state by default, because the raw NOAH-vs-market gap has opposite signs in
MD and DC; pooling would average that away. A pooled model with jurisdiction fixed
effects is also available.

Run::

    uv run python -m greengap.type_regression run --outcome canopy_pct
    uv run python -m greengap.type_regression run --outcome mean_lst --state DC

Continuous controls are standardised (mean 0, sd 1) so their coefficients are
comparable and the intercept stays interpretable; the outcome and the type dummies
are left in natural units.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import typer

app = typer.Typer(help=__doc__, no_args_is_help=True)

# Housing types entered as dummies; market-rate is the omitted baseline so each
# coefficient is a contrast against market-rate multifamily.
TYPE_BASELINE = "market_rate"
TYPE_TERMS = ["noah", "subsidised"]

# Continuous controls, standardised before fitting. log1p for the heavy-tailed
# size variables; the ACS covariates enter on their natural scale then standardised.
LOG_CONTROLS = ["lot_area", "units"]
LINEAR_CONTROLS = [
    "year_built", "median_income", "poverty_rate",
    "pct_bachelors_plus", "pct_black_nh", "pct_hispanic", "pop_density",
]
# Flood (FEMA NFHL) is intentionally omitted: the current NFHL pull is partial
# (~4.4k of ~19.7k SFHA polygons), so in_floodplain would be an undercount. Re-add
# once a complete flood layer is loaded.


def _standardise(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    return (s - s.mean()) / sd if sd and not np.isnan(sd) else s * 0.0


def build_design(df: pd.DataFrame, outcome: str, with_controls: bool) -> pd.DataFrame:
    """Assemble the modelling frame: outcome, type dummies, and (optionally) controls."""
    keep = df["housing_type"].isin([TYPE_BASELINE] + TYPE_TERMS)
    d = df.loc[keep].copy()

    out = pd.DataFrame(index=d.index)
    out[outcome] = pd.to_numeric(d[outcome], errors="coerce")
    for t in TYPE_TERMS:
        out[f"type_{t}"] = (d["housing_type"] == t).astype(float)

    if with_controls:
        for c in LOG_CONTROLS:
            out[c] = _standardise(np.log1p(pd.to_numeric(d[c], errors="coerce")))
        for c in LINEAR_CONTROLS:
            if c in d.columns:
                out[c] = _standardise(pd.to_numeric(d[c], errors="coerce"))
        # Jurisdiction fixed effects (drop first to avoid collinearity with const).
        juris = pd.get_dummies(d["jurisdiction"], prefix="j", drop_first=True, dtype=float)
        out = pd.concat([out, juris], axis=1)

    return out.dropna()


def fit(df: pd.DataFrame, outcome: str, with_controls: bool):
    """Fit OLS with HC3 robust SEs; returns the fitted statsmodels result."""
    import statsmodels.api as sm

    design = build_design(df, outcome, with_controls).astype("float64")
    y = design[outcome]
    x = sm.add_constant(design.drop(columns=[outcome]), has_constant="add")
    return sm.OLS(y, x).fit(cov_type="HC3")


def type_gaps(df: pd.DataFrame, outcome: str, with_controls: bool) -> pd.DataFrame:
    """Return the NOAH- and subsidised-vs-market coefficients with 95% CIs."""
    res = fit(df, outcome, with_controls)
    rows = []
    for t in TYPE_TERMS:
        term = f"type_{t}"
        if term not in res.params:
            continue
        ci = res.conf_int().loc[term]
        rows.append({
            "type": t,
            "gap_vs_market": res.params[term],
            "ci_low": ci[0],
            "ci_high": ci[1],
            "se": res.bse[term],
            "p": res.pvalues[term],
        })
    out = pd.DataFrame(rows)
    out["outcome"] = outcome
    out["adjusted"] = with_controls
    out["n"] = int(res.nobs)
    return out


def run_all(df: pd.DataFrame, outcome: str) -> pd.DataFrame:
    """Unadjusted and adjusted gaps, per state and pooled, stacked into one table."""
    frames = []
    for label, sub in [("MD", df[df["state"] == "MD"]),
                       ("DC", df[df["state"] == "DC"]),
                       ("pooled", df)]:
        for adj in (False, True):
            g = type_gaps(sub, outcome, with_controls=adj)
            g.insert(0, "scope", label)
            frames.append(g)
    return pd.concat(frames, ignore_index=True)


@app.command("run")
def run_cmd(
    outcome: str = typer.Option("canopy_pct", help="Environmental outcome column."),
    state: str = typer.Option(None, help="Limit to MD or DC (default: all + pooled)."),
):
    """Print the type-contrast gaps for one environmental outcome."""
    from greengap.type_regression_data import load_regression_frame

    df = load_regression_frame()
    if state:
        df = df[df["state"] == state]
        out = pd.concat([
            type_gaps(df, outcome, False).assign(scope=state, adjusted=False),
            type_gaps(df, outcome, True).assign(scope=state, adjusted=True),
        ])
    else:
        out = run_all(df, outcome)
    cols = ["scope", "type", "adjusted", "gap_vs_market", "ci_low", "ci_high", "p", "n"]
    print(out[cols].round(3).to_string(index=False))


if __name__ == "__main__":
    app()

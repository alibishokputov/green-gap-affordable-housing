"""Univariate class breaks (quantiles / equal interval / natural breaks).

A dependency-free stand-in for the slice of ``mapclassify`` this project needs.

Why not just use mapclassify? It imports ``matplotlib`` at module scope, and
importing matplotlib under Pyodide kicks off a font-cache build that never
finishes - the WebAssembly dashboard hangs on a blank page instead of loading.
Dropping mapclassify from the app also drops scikit-learn and networkx from the
browser payload.

``natural_breaks`` here is an exact Fisher-Jenks optimal partition (dynamic
programming), and ``tests/test_classify.py`` asserts these functions agree with
mapclassify on the real project data, so the published map and the local map
cannot silently diverge.

Only numpy is required.
"""

from __future__ import annotations

import numpy as np

SCHEMES = ("quantiles", "equal_interval", "natural_breaks")


def _clean(values: np.ndarray) -> np.ndarray:
    v = np.asarray(values, dtype="float64")
    return v[~np.isnan(v)]


def quantile_bins(values: np.ndarray, k: int = 3) -> np.ndarray:
    """Upper edge of each of ``k`` equal-count classes."""
    v = _clean(values)
    qs = np.linspace(0, 100, k + 1)[1:]
    return np.percentile(v, qs)


def equal_interval_bins(values: np.ndarray, k: int = 3) -> np.ndarray:
    """Upper edge of each of ``k`` equal-width classes."""
    v = _clean(values)
    lo, hi = float(v.min()), float(v.max())
    return lo + (hi - lo) * np.arange(1, k + 1) / k


def natural_breaks_bins(values: np.ndarray, k: int = 3) -> np.ndarray:
    """Upper edge of each class under an exact Fisher-Jenks partition.

    Minimises the total within-class sum of squares via dynamic programming over
    the sorted values, using prefix sums so each candidate split is O(1).
    """
    v = np.sort(_clean(values))
    n = v.size
    if n == 0:
        raise ValueError("no finite values to classify")
    distinct = np.unique(v)
    if distinct.size <= k:
        # Fewer distinct values than classes: every value is its own class.
        out = np.full(k, distinct[-1], dtype="float64")
        out[: distinct.size] = distinct
        return out

    cs = np.concatenate(([0.0], np.cumsum(v)))
    cs2 = np.concatenate(([0.0], np.cumsum(v**2)))

    def sse(i: np.ndarray, j: int) -> np.ndarray:
        """Within-class SSE of v[i:j] for each start ``i`` (vectorised)."""
        cnt = j - i
        s = cs[j] - cs[i]
        s2 = cs2[j] - cs2[i]
        return s2 - (s * s) / np.maximum(cnt, 1)

    dp = np.full((k + 1, n + 1), np.inf)
    split = np.zeros((k + 1, n + 1), dtype="int64")
    dp[0, 0] = 0.0

    for m in range(1, k + 1):
        for j in range(m, n + 1):
            starts = np.arange(m - 1, j)
            costs = dp[m - 1, starts] + sse(starts, j)
            best = int(np.argmin(costs))
            dp[m, j] = costs[best]
            split[m, j] = starts[best]

    # Walk the splits back to class boundaries, then read off upper edges.
    edges: list[float] = []
    j = n
    for m in range(k, 0, -1):
        edges.append(float(v[j - 1]))
        j = int(split[m, j])
    edges.reverse()
    return np.asarray(edges, dtype="float64")


def bins(values: np.ndarray, scheme: str = "natural_breaks", k: int = 3) -> np.ndarray:
    """Class upper edges for ``scheme``; mirrors ``mapclassify.classify(...).bins``."""
    if scheme == "quantiles":
        return quantile_bins(values, k)
    if scheme == "equal_interval":
        return equal_interval_bins(values, k)
    if scheme == "natural_breaks":
        return natural_breaks_bins(values, k)
    raise ValueError(f"unknown scheme {scheme!r}; expected one of {SCHEMES}")


def classify(values: np.ndarray, scheme: str = "natural_breaks", k: int = 3) -> np.ndarray:
    """Class index in ``[0, k)`` for each value; NaN falls in class 0."""
    edges = bins(values, scheme, k)
    v = np.asarray(values, dtype="float64")
    out = np.clip(np.digitize(v, edges[:-1], right=True), 0, k - 1)
    out[np.isnan(v)] = 0
    return out

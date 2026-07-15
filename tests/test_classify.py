"""greengap.classify must agree with mapclassify.

The app cannot import mapclassify (it pulls in matplotlib, which hangs Pyodide),
so we reimplemented the breaks. These tests pin that reimplementation to
mapclassify's output: if they ever disagree, the published WebAssembly dashboard
would draw a different map than the local one from identical data.

mapclassify stays a project dependency for notebooks, so it is available here.
"""

import numpy as np
import pytest

from greengap.classify import bins, classify

mapclassify = pytest.importorskip("mapclassify")


def _zero_inflated(n_zero=1000, n_pos=475, seed=0):
    """Mimics the real LIHTC distribution: ~2/3 of tracts have no units."""
    rng = np.random.default_rng(seed)
    return np.concatenate([np.zeros(n_zero), rng.integers(10, 800, n_pos)]).astype(float)


def _canopy_like(n=1666, seed=1):
    rng = np.random.default_rng(seed)
    return np.clip(rng.beta(2, 2, n) * 100, 0, 100)


def _within_class_sse(edges: np.ndarray, values: np.ndarray) -> float:
    """Total within-class sum of squares - the quantity Jenks minimises."""
    v = np.sort(values)
    k = np.clip(np.digitize(v, edges[:-1], right=True), 0, len(edges) - 1)
    total = 0.0
    for c in np.unique(k):
        group = v[k == c]
        total += float(((group - group.mean()) ** 2).sum())
    return total


@pytest.mark.parametrize(
    ("scheme", "data_fn"),
    [
        ("quantiles", _canopy_like),
        ("equal_interval", _canopy_like),
        ("equal_interval", _zero_inflated),
        # ("quantiles", _zero_inflated) is a deliberate divergence - see below.
    ],
)
def test_deterministic_schemes_assign_like_mapclassify(scheme, data_fn):
    # Compare class assignments, not `bins`: mapclassify drops duplicate cut
    # points, so the arrays can differ in shape while classifying identically.
    values = data_fn()
    np.testing.assert_array_equal(
        classify(values, scheme, k=3),
        mapclassify.classify(values, scheme, k=3).yb,
    )


def test_degenerate_quantiles_put_nonzero_values_in_the_top_class():
    """Deliberate divergence from mapclassify on zero-inflated data.

    With ~2/3 zeros both quantile cut points land on 0. mapclassify compacts its
    bins and assigns the non-zero values to class 1 of 3, leaving the top class
    empty - which is what made the dashboard report zero green-gap tracts under
    quantiles. We keep k edges, so non-zero values land in the top class, where a
    reader expects "the tracts with the most LIHTC" to be. Both agree the axis
    has collapsed to two groups; we just put the occupied group at the top.
    """
    values = _zero_inflated()
    ours = classify(values, "quantiles", k=3)
    assert set(np.unique(ours)) == {0, 2}
    assert (ours[values == 0] == 0).all()
    assert (ours[values > 0] == 2).all()


@pytest.mark.parametrize("data_fn", [_zero_inflated, _canopy_like])
def test_natural_breaks_is_at_least_as_good_as_mapclassify(data_fn):
    # Not an equality test on purpose: mapclassify's NaturalBreaks is k-means
    # with random init, so it is stochastic and can land on a sub-optimal
    # partition. Ours is an exact Fisher-Jenks DP, so its within-class SSE must
    # always be <= theirs. (In practice they usually coincide.)
    values = data_fn()
    ours = bins(values, "natural_breaks", k=3)
    theirs = mapclassify.classify(values, "natural_breaks", k=3).bins
    assert _within_class_sse(ours, values) <= _within_class_sse(theirs, values) + 1e-6


def test_natural_breaks_is_deterministic():
    values = _canopy_like()
    first = bins(values, "natural_breaks", k=3)
    for _ in range(3):
        np.testing.assert_array_equal(bins(values, "natural_breaks", k=3), first)


def test_natural_breaks_beats_quantiles_on_zero_inflated_data():
    # The whole reason natural breaks is the app default: quantile cut points
    # both land on 0 here, collapsing the axis and emptying the top class.
    values = _zero_inflated()
    q = classify(values, "quantiles", k=3)
    nb = classify(values, "natural_breaks", k=3)
    assert len(np.unique(q)) < 3, "expected quantiles to degenerate"
    assert len(np.unique(nb)) == 3, "natural breaks should keep 3 usable classes"


def test_nan_goes_to_class_zero_and_does_not_crash():
    values = np.array([1.0, 2.0, np.nan, 50.0, 99.0, 3.0])
    out = classify(values, "natural_breaks", k=3)
    assert out[2] == 0
    assert out.shape == values.shape


def test_fewer_distinct_values_than_classes():
    out = bins(np.array([5.0, 5.0, 5.0]), "natural_breaks", k=3)
    assert out.shape == (3,)
    assert np.all(out == 5.0)


def test_unknown_scheme_raises():
    with pytest.raises(ValueError, match="unknown scheme"):
        bins(np.arange(10.0), "not_a_scheme")

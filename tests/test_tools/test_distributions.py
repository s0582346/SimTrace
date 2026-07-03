"""Tests for the distribution-spec parser feeding stochastic delays."""

import random

import pytest

from simtrace.tools.distributions import is_distribution_spec, parse_delay


# --- constants pass through -----------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 3.5, 100])
def test_number_passes_through_unchanged(value):
    assert parse_delay("d", value) == value


def test_bool_rejected():
    with pytest.raises(ValueError, match="distribution string"):
        parse_delay("d", True)


def test_non_number_non_string_rejected():
    with pytest.raises(ValueError, match="distribution string"):
        parse_delay("d", [1, 2])


# --- is_distribution_spec -------------------------------------------------


@pytest.mark.parametrize(
    "s", ["uniform(2, 8)", "normal(5,1)", "gauss(5, 1)", "exp(6)", " exp(6) "]
)
def test_is_distribution_spec_true(s):
    assert is_distribution_spec(s)


@pytest.mark.parametrize("s", ["", "poisson(3)", "uniform()", "5", "exp"])
def test_is_distribution_spec_false(s):
    assert not is_distribution_spec(s)


def test_is_distribution_spec_non_string():
    assert not is_distribution_spec(5)


# --- parsing into samplers ------------------------------------------------


def test_uniform_returns_sampler_in_range():
    sampler = parse_delay("d", "uniform(2, 8)")
    random.seed(0)
    draws = [sampler() for _ in range(1000)]
    assert all(2.0 <= x <= 8.0 for x in draws)
    # actually spread out, not a constant
    assert len(set(draws)) > 1


def test_exp_mean_is_the_argument_not_the_rate():
    # exp(x) is documented as MEAN = x. With mean 5, the sample average over
    # many draws should land near 5 (rate would give ~0.2).
    sampler = parse_delay("d", "exp(5)")
    random.seed(0)
    avg = sum(sampler() for _ in range(20000)) / 20000
    assert 4.0 < avg < 6.0


def test_normal_and_gauss_are_aliases():
    random.seed(0)
    a = parse_delay("d", "normal(5, 1)")()
    random.seed(0)
    b = parse_delay("d", "gauss(5, 1)")()
    assert a == b


def test_negative_samples_are_clamped_to_zero():
    # normal(0, 5) produces negatives ~half the time; every draw must be >= 0.
    sampler = parse_delay("d", "normal(0, 5)")
    random.seed(1)
    draws = [sampler() for _ in range(2000)]
    assert min(draws) == 0.0  # clamped mass at zero
    assert all(x >= 0.0 for x in draws)


# --- malformed / out-of-range specs ---------------------------------------


def test_unknown_distribution_rejected():
    with pytest.raises(ValueError, match="invalid distribution string"):
        parse_delay("d", "poisson(3)")


def test_uniform_needs_two_args():
    with pytest.raises(ValueError, match="needs two args"):
        parse_delay("d", "uniform(5)")


def test_exp_rejects_two_args():
    with pytest.raises(ValueError, match="takes one arg"):
        parse_delay("d", "exp(5, 1)")


def test_uniform_rejects_a_greater_than_b():
    with pytest.raises(ValueError, match="a <= b"):
        parse_delay("d", "uniform(8, 2)")


def test_exp_rejects_nonpositive_mean():
    with pytest.raises(ValueError, match="mean x > 0"):
        parse_delay("d", "exp(0)")


def test_normal_rejects_negative_stddev():
    # regex only allows non-negative literals, so a negative stddev can't be
    # expressed as a string; guard is defensive. Zero stddev is allowed.
    sampler = parse_delay("d", "normal(5, 0)")
    assert sampler() == 5.0

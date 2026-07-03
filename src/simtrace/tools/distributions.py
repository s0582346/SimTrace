"""Parse distribution-spec strings into zero-arg delay samplers.

A delay is either a constant number (returned unchanged) or a distribution
string parsed into a zero-arg sampler:

    uniform(a, b)   -> random.uniform(a, b)
    normal(m, s)    -> random.gauss(m, s)      (mean m, stddev s)
    gauss(m, s)     -> random.gauss(m, s)      (alias of normal)
    exp(x)          -> random.expovariate(1 / x)   (mean x, not rate)

Every sampler is clamped non-negative (`max(0.0, sample)`).
"""

from __future__ import annotations

import random
import re
from typing import Callable, Union

# A distribution spec is a name followed by 1 or 2 numeric args in parens.
# Mirrors the JSON-schema pattern; whitespace around tokens is tolerated.
_SPEC_RE = re.compile(
    r"^\s*(?P<name>uniform|normal|gauss|exp)\s*\(\s*"
    r"(?P<a>[0-9]*\.?[0-9]+)\s*"
    r"(?:,\s*(?P<b>[0-9]*\.?[0-9]+)\s*)?"
    r"\)\s*$"
)

# name -> (arg count, factory(a, b) -> zero-arg sampler). The sampler is wrapped
# in the non-negativity clamp by _clamped() below, so factories here return the
# raw draw.
_TWO_ARG = {"uniform", "normal", "gauss"}
_ONE_ARG = {"exp"}

DelaySpec = Union[int, float, str]


def _clamped(sampler: Callable[[], float]) -> Callable[[], float]:
    """Wrap a sampler so it never returns a negative value."""

    def draw() -> float:
        return max(0.0, sampler())

    return draw


def is_distribution_spec(value: object) -> bool:
    """True if `value` is a string that parses as a distribution spec."""
    return isinstance(value, str) and _SPEC_RE.match(value) is not None


def parse_delay(name: str, value: DelaySpec) -> Union[int, float, Callable[[], float]]:
    """Resolve a delay param to a constant or a zero-arg sampler.

    Args:
        name: parameter name, used in error messages.
        value: a constant int/float (returned unchanged) or a distribution
            string (returned as a clamped zero-arg callable).

    Returns:
        The number itself, or a callable that FactorySimPy's `get_delay` will
        invoke once per cycle.

    Raises:
        ValueError: if `value` is neither a number nor a well-formed,
            in-range distribution string. `bool` is rejected (it is an `int`
            subclass, so `True`/`False` would otherwise slip through as 1/0).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(
            f"{name} must be a constant int/float or a distribution string "
            f'(e.g. "uniform(2, 8)", "exp(6)"); got {value!r}.'
        )
    if isinstance(value, (int, float)):
        return value

    match = _SPEC_RE.match(value)
    if match is None:
        raise ValueError(
            f"{name}: invalid distribution string {value!r}. Expected one of "
            'uniform(a, b), normal(m, s), gauss(m, s), exp(x).'
        )

    dist = match.group("name")
    a = float(match.group("a"))
    b = match.group("b")
    b = float(b) if b is not None else None

    if dist in _TWO_ARG:
        if b is None:
            raise ValueError(f"{name}: {dist}(...) needs two args, e.g. {dist}(2, 8).")
    elif dist in _ONE_ARG:
        if b is not None:
            raise ValueError(f"{name}: {dist}(...) takes one arg, e.g. {dist}(6).")

    if dist == "uniform":
        if a > b:
            raise ValueError(f"{name}: uniform(a, b) needs a <= b (got {a}, {b}).")
        return _clamped(lambda: random.uniform(a, b))
    if dist in ("normal", "gauss"):
        if b < 0:
            raise ValueError(f"{name}: {dist}(m, s) needs stddev s >= 0 (got {b}).")
        return _clamped(lambda: random.gauss(a, b))
    if dist == "exp":
        # Schema convention: exp(x) has MEAN x, so rate = 1/x.
        if a <= 0:
            raise ValueError(f"{name}: exp(x) needs mean x > 0 (got {a}).")
        return _clamped(lambda: random.expovariate(1.0 / a))

    # Unreachable: _SPEC_RE only matches the names handled above.
    raise ValueError(f"{name}: unsupported distribution {dist!r}.")

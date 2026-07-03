"""Shared helpers for the node/edge builders."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Union

from simtrace.tools.distributions import parse_delay

if TYPE_CHECKING:
    from simtrace.model import FactoryModel


def require_number(name: str, value: object) -> None:
    """Validate that `value` is a constant int or float.

    This is a type guard for delay/length params, not a presence check —
    whether an argument is supplied is governed by the builder's signature
    defaults. `bool` is rejected explicitly (it is an `int` subclass, so
    `True`/`False` would otherwise pass). Generators and callables are a
    later extension and are rejected for now.

    Args:
        name: parameter name, used in the error message.
        value: the value to validate.

    Raises:
        ValueError: if `value` is not a plain int or float.
    """
    # bool is an int subclass; exclude it so True/False aren't silently accepted.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{name} must be a constant int or float (got {value!r}). "
            "Generators/callables are a later extension."
        )


def resolve_delay(
    name: str, value: object
) -> Union[int, float, Callable[[], float]]:
    """Validate + resolve a delay param that may be constant OR stochastic.

    Unlike `require_number` (constant-only), this accepts either a plain
    int/float or a distribution string (`uniform(a, b)`, `normal(m, s)`,
    `gauss(m, s)`, `exp(x)`) and returns what FactorySimPy should receive: the
    number unchanged, or a zero-arg sampler callable for a distribution string.

    The builder should pass the *return value* to the FactorySimPy constructor
    but echo the *original* `value` in its summary dict — a callable is not
    JSON-serializable, whereas the spec string is.

    Raises:
        ValueError: if `value` is neither a number nor a well-formed
            distribution string (delegated to `parse_delay`).
    """
    return parse_delay(name, value)


def require_positive_int(name: str, value: object) -> None:
    """Validate that `value` is a positive int (>= 1).

    `bool` is an `int` subclass, so exclude it explicitly — otherwise
    True/False would slip through as a count.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int (got {value!r}).")
    if value < 1:
        raise ValueError(f"{name} must be a positive int (>= 1).")


def require_positive_number(name: str, value: object) -> None:
    """Validate that `value` is a constant int/float and strictly positive."""
    require_number(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be > 0 (got {value!r}).")


def require_unique_id(id: str, model: FactoryModel) -> None:
    """Validate a component id: non-empty string, not already taken.

    Ids are global across nodes and edges (see `FactoryModel.has_id`), so this
    rejects an id used by either registry.
    """
    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_id(id):
        raise ValueError(f"A node or edge with id '{id}' already exists.")

"""Shared helpers for the node/edge builders."""

from __future__ import annotations


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

"""Units in the tool arguments, for a model built with strict_units.

With strict_units on, every time, length and speed argument of the create_*
tools and every `until` of the run tools is written with its unit, e.g.
"uniform(3 min, 4 min)", "0.3 m", "20 parts/min * 0.3 m", "2400 min". The
dimension test after Rabe et al. runs in the call: the argument's dimension
must be the one the setting needs, else the call is refused and nothing is
built. A rate for a time setting is refused too; "1 / (12 parts/h)" is a time
and is accepted. The argument is then converted into the model's units, so the
builders and the build log get plain numbers as before. Counts and shares stay
bare numbers. With strict_units off, arguments pass through unchanged.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from simtrace.model import get_model as get_session_model
from simtrace.model import unit_settings
from simtrace.tools.validation.dimensions import (
    _COUNT,
    _TIME,
    FIELDS,
    Figure,
    dim_name,
    parse_figure,
)

TIME_UNITS = {"s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}
LENGTH_UNITS = {"mm": 0.001, "cm": 0.01, "m": 1.0}


def _number_text(x: float) -> str:
    """A number as the distribution strings accept it: no exponent."""
    text = repr(float(x))
    if "e" in text or "E" in text:
        text = f"{x:.15f}".rstrip("0").rstrip(".")
    return text


def _scalar_dim(label: str, raw: object, figure: Figure, dim: tuple[int, int]) -> None:
    """Refuse a figure whose dimension is not `dim`. A bare 0 fits any dimension."""
    for scalar in figure.args:
        q = scalar.quantity
        if q.value == 0 and q.dim == _COUNT:
            continue
        if q.dim != dim:
            raise ValueError(
                f"{label}: {raw} is {dim_name(q.dim)}, the setting needs {dim_name(dim)}."
            )


def convert(label: str, raw: object, dim: tuple[int, int], model=None) -> object:
    """`raw` in the model's units, after checking its dimension.

    Returns `raw` unchanged when the model has strict_units off, for counts,
    for None and booleans, and for a bare 0.
    """
    model = model if model is not None else get_session_model()
    if not model.strict_units or dim == _COUNT:
        return raw
    if raw is None or isinstance(raw, bool):
        return raw
    if isinstance(raw, (list, tuple)):
        return [convert(label, item, dim, model) for item in raw]
    if isinstance(raw, (int, float)):
        if raw == 0:
            return raw
        raise ValueError(
            f"{label}: {raw!r} has no unit. With strict_units on, a "
            f"{dim_name(dim)} setting needs one."
        )
    try:
        figure = parse_figure(raw)
    except ValueError as exc:
        raise ValueError(f"{label}: cannot read {raw!r}: {exc}.") from None
    if figure.kind == "list":
        raise ValueError(f"{label}: {raw!r} is a list, the setting needs one quantity.")
    _scalar_dim(label, raw, figure, dim)
    scale = TIME_UNITS[model.time_unit] ** dim[0] * LENGTH_UNITS[model.length_unit] ** dim[1]
    numbers = [s.quantity.value / scale for s in figure.args]
    if figure.kind == "constant":
        return numbers[0]
    return f"{figure.kind}({', '.join(_number_text(n) for n in numbers)})"


def strict(op: str | None = None, extra: dict[str, tuple[int, int]] | None = None) -> Callable:
    """Decorator for a tool wrapper: convert the unit arguments the caller passed.

    `op` names the create_* step whose settings in FIELDS are converted;
    `extra` adds more arguments, e.g. {"until": time}. Only arguments the caller
    actually passed are converted, so defaults stay as they are. For a create_*
    step the text as written is kept on the model under `written[id]`. A run
    tool's result gets `time_unit` when the model has strict_units on.
    """
    settings = dict(FIELDS.get(op, {})) if op else {}
    settings.update(extra or {})

    def decorate(fn: Callable) -> Callable:
        signature = inspect.signature(fn)
        note = _description_note(op) if unit_settings()["strict_units"] else ""

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            model = get_session_model()
            bound = signature.bind_partial(*args, **kwargs)
            component = bound.arguments.get("id")
            written = {}
            for name, dim in settings.items():
                if name not in bound.arguments:
                    continue
                raw = bound.arguments[name]
                label = f"{component}.{name}" if component else name
                bound.arguments[name] = convert(label, raw, dim, model)
                if model.strict_units and isinstance(raw, (str, list)):
                    written[name] = raw
            result = fn(*bound.args, **bound.kwargs)
            if component and written:
                model.written.setdefault(component, {}).update(written)
            if model.strict_units and op is None and isinstance(result, dict):
                result["time_unit"] = model.time_unit
            return result

        if note:
            wrapper.__doc__ = inspect.cleandoc(fn.__doc__ or "") + "\n\n" + note
        return wrapper

    return decorate


def _description_note(op: str | None) -> str:
    """The sentence added to a tool's description when strict units are on."""
    if op is None:
        return ('Units: this server needs the unit on `until`, e.g. "10 h". '
                f"Results are in {unit_settings()['time_unit']}.")
    return ('Units: this server needs the unit on every time, length and speed '
            'argument, e.g. "4 min", "uniform(50 s, 75 s)", "0.6 m", "1.5 km/h". '
            "Counts and shares are plain numbers. A rate is not a time: write the "
            'inversion, e.g. "1 / (40 /d)". A setting calculated from several '
            "sheet values is written as the calculation.")


UNTIL = {"until": _TIME}

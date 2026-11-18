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

Parts are counted, not measured: words like parts, items or pallets carry no
dimension, so "12 parts/h" is [1/time].
"""

from __future__ import annotations

import functools
import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable

from simtrace.model import get_model as get_session_model
from simtrace.model import unit_settings
from simtrace.tools.distributions import unquote

TIME_UNITS = {"s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}
LENGTH_UNITS = {"mm": 0.001, "cm": 0.01, "m": 1.0}

# Dimension = (exponent of time, exponent of length).
_TIME = (1, 0)
_LENGTH = (0, 1)
_SPEED = (-1, 1)
_RATE = (-1, 0)
_COUNT = (0, 0)

# Every word an argument may use, as (seconds, metres) factor and its dimension.
_WORDS: dict[str, tuple[float, tuple[int, int]]] = {}
for _names, _factor in (
    (("s", "sec", "secs", "second", "seconds"), 1.0),
    (("min", "mins", "minute", "minutes"), 60.0),
    (("h", "hr", "hrs", "hour", "hours"), 3600.0),
    (("d", "day", "days"), 86400.0),
):
    for _name in _names:
        _WORDS[_name] = (_factor, _TIME)
for _names, _factor in (
    (("mm", "millimetre", "millimetres", "millimeter", "millimeters"), 0.001),
    (("cm", "centimetre", "centimetres", "centimeter", "centimeters"), 0.01),
    (("dm",), 0.1),
    (("m", "metre", "metres", "meter", "meters"), 1.0),
    (("km",), 1000.0),
):
    for _name in _names:
        _WORDS[_name] = (_factor, _LENGTH)
for _name in ("part", "parts", "item", "items", "piece", "pieces", "pcs", "pc",
              "pallet", "pallets", "places", "place", "unit", "units",
              "failure", "failures", "breakdown", "breakdowns", "order", "orders",
              "housing", "housings", "shaft", "shafts", "box", "boxes",
              "carton", "cartons", "crate", "crates", "package", "packages",
              "bag", "bags", "batch", "batches", "lot", "lots", "job", "jobs",
              "trip", "trips"):
    _WORDS[_name] = (1.0, _COUNT)
_WORDS["%"] = (0.01, _COUNT)

# Unit settings per create_* tool and the dimension each needs.
SETTINGS: dict[str, dict[str, tuple[int, int]]] = {
    "create_source": {"inter_arrival_time": _TIME, "item_length": _LENGTH,
                      "node_setup_time": _TIME},
    "create_sink": {"node_setup_time": _TIME},
    "create_machine": {"processing_delay": _TIME, "node_setup_time": _TIME,
                       "time_to_failure": _TIME, "repair_time": _TIME,
                       "work_capacity": _COUNT},
    "create_splitter": {"processing_delay": _TIME, "node_setup_time": _TIME,
                        "split_quantity": _COUNT},
    "create_combiner": {"processing_delay": _TIME, "node_setup_time": _TIME},
    "create_buffer": {"delay": _TIME, "capacity": _COUNT},
    "create_conveyor": {"conveyor_length": _LENGTH, "speed": _SPEED,
                        "item_length": _LENGTH},
    "create_fleet": {"delay": _TIME, "transit_delay": _TIME, "capacity": _COUNT},
    "create_shift_calendar": {"cycle": _TIME, "windows": _TIME},
}

UNTIL = {"until": _TIME}


# --- reading an argument -----------------------------------------------------

@dataclass(frozen=True)
class Quantity:
    """A number in seconds and metres with its dimension."""

    value: float
    dim: tuple[int, int]

    def __mul__(self, other: Quantity) -> Quantity:
        return Quantity(self.value * other.value,
                        (self.dim[0] + other.dim[0], self.dim[1] + other.dim[1]))

    def __truediv__(self, other: Quantity) -> Quantity:
        if other.value == 0:
            raise ValueError("division by zero")
        return Quantity(self.value / other.value,
                        (self.dim[0] - other.dim[0], self.dim[1] - other.dim[1]))

    def power(self, exponent: int) -> Quantity:
        return Quantity(self.value ** exponent,
                        (self.dim[0] * exponent, self.dim[1] * exponent))

    def add(self, other: Quantity, sign: float) -> Quantity:
        if self.dim != other.dim:
            raise ValueError(
                f"a sum of {dim_name(self.dim)} and {dim_name(other.dim)}"
            )
        return Quantity(self.value + sign * other.value, self.dim)


@dataclass(frozen=True)
class Expression:
    """A read argument: a constant or a distribution of quantities."""

    kind: str  # "constant", "uniform", "normal" or "exp"
    args: tuple[Quantity, ...]


_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)"
    r"|(?P<word>[A-Za-z%]+)"
    r"|(?P<op>\*\*|[*/^()+\-·×]))"
)

# Superscript powers, e.g. m/s², and their ^ form.
_SUPERSCRIPTS = (("⁻¹", "^-1"), ("⁻²", "^-2"), ("¹", "^1"), ("²", "^2"), ("³", "^3"))


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    text = text.strip()
    for sign, power in _SUPERSCRIPTS:
        text = text.replace(sign, power)
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise ValueError(f"unexpected text at {text[position:]!r}")
        position = match.end()
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "word" and value.lower() == "per":
            kind, value = "op", "per"
        tokens.append((kind, value))
    return tokens


class _Parser:
    """expr := term ((+|-|nothing) term)*, term := power ((*|/|nothing) power)*,
    power := unary (^ integer)?, unary := -? atom,
    atom := number | unit word | ( expr ).

    A number right after a unit word starts a new summand, so "2 h 30 min"
    is 2 h + 30 min. "per" divides by the whole term after it, so
    "3 per 8 h" is 3 / (8 h)."""

    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text)
        self.at = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.tokens[self.at]
        self.at += 1
        return token

    def parse(self) -> Quantity:
        if not self.tokens:
            raise ValueError("empty argument")
        quantity = self.expr()
        if self.peek() is not None:
            raise ValueError(f"unexpected {self.peek()[1]!r}")
        return quantity

    def number_after_unit(self) -> bool:
        token = self.peek()
        return (token is not None and token[0] == "num" and self.at > 0
                and self.tokens[self.at - 1][0] == "word")

    def expr(self) -> Quantity:
        result = self.term()
        while True:
            if self.peek() in (("op", "+"), ("op", "-")):
                sign = 1.0 if self.take()[1] == "+" else -1.0
            elif self.number_after_unit():
                sign = 1.0
            else:
                return result
            result = result.add(self.term(), sign)

    def term(self) -> Quantity:
        result = self.power()
        while True:
            token = self.peek()
            if self.number_after_unit():
                return result
            if token in (("op", "*"), ("op", "·"), ("op", "×")):
                self.take()
                result = result * self.power()
            elif token == ("op", "/"):
                self.take()
                # A number after "/" takes the unit words after it, so
                # "6 m / 1 min" is 6 m / (1 min).
                starts_with_number = self.peek() is not None and self.peek()[0] == "num"
                divisor = self.power()
                while starts_with_number and self.peek() is not None and self.peek()[0] == "word":
                    divisor = divisor * self.power()
                result = result / divisor
            elif token == ("op", "per"):
                self.take()
                result = result / self.term()
            elif token is not None and (token[0] in ("num", "word") or token == ("op", "(")):
                result = result * self.power()
            else:
                return result

    def power(self) -> Quantity:
        base = self.unary()
        if self.peek() in (("op", "^"), ("op", "**")):
            self.take()
            negative = self.peek() == ("op", "-")
            if negative:
                self.take()
            token = self.take()
            if token[0] != "num" or not float(token[1]).is_integer():
                raise ValueError("an exponent must be a whole number")
            exponent = int(float(token[1]))
            base = base.power(-exponent if negative else exponent)
        return base

    def unary(self) -> Quantity:
        if self.peek() == ("op", "-"):
            self.take()
            inner = self.unary()
            return Quantity(-inner.value, inner.dim)
        return self.atom()

    def atom(self) -> Quantity:
        if self.peek() is None:
            raise ValueError("argument ends too early")
        kind, text = self.take()
        if kind == "num":
            return Quantity(float(text), _COUNT)
        if kind == "word":
            if text.lower() not in _WORDS:
                raise ValueError(f"unknown unit {text!r}")
            factor, dim = _WORDS[text.lower()]
            return Quantity(factor, dim)
        if text == "(":
            inner = self.expr()
            if self.peek() != ("op", ")"):
                raise ValueError("missing )")
            self.take()
            return inner
        raise ValueError(f"unexpected {text!r}")


def _split_top(text: str) -> list[str]:
    """Split at the commas that are outside any brackets."""
    parts, depth, start = [], 0, 0
    for i, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [part.strip() for part in parts]


_DIST = re.compile(r"^\s*(uniform|normal|gauss|exp)\s*\((.*)\)\s*$", re.S | re.I)
_ARG_COUNT = {"uniform": 2, "normal": 2, "exp": 1}


def read(text: str) -> Expression:
    """Read an argument with its units. Raises ValueError with the reason."""
    stripped = unquote(text)
    match = _DIST.match(stripped)
    if match is not None:
        name = match.group(1).lower()
        name = "normal" if name == "gauss" else name
        args = _split_top(match.group(2))
        if len(args) != _ARG_COUNT[name]:
            raise ValueError(f"{name}(...) takes {_ARG_COUNT[name]} argument(s)")
        return Expression(name, tuple(_Parser(arg).parse() for arg in args))
    return Expression("constant", (_Parser(stripped).parse(),))


def dim_name(dim: tuple[int, int]) -> str:
    names = {_TIME: "[time]", _LENGTH: "[length]", _SPEED: "[length/time]",
             _RATE: "[1/time]", _COUNT: "[none]"}
    return names.get(dim, f"[time^{dim[0]} length^{dim[1]}]")


# --- the check in each call ---------------------------------------------------

def _number_text(x: float) -> str:
    """A number as the distribution strings accept it: no exponent."""
    text = repr(float(x))
    if "e" in text or "E" in text:
        text = f"{x:.15f}".rstrip("0").rstrip(".")
    return text


def _check_dim(label: str, raw: object, expression: Expression, dim: tuple[int, int]) -> None:
    """Refuse an argument whose dimension is not `dim`. A bare 0 fits any dimension."""
    for q in expression.args:
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
    if not isinstance(raw, str):
        raise ValueError(f"{label}: {raw!r} must be text such as '5 min'.")
    try:
        expression = read(raw)
    except ValueError as exc:
        raise ValueError(f"{label}: cannot read {raw!r}: {exc}.") from None
    _check_dim(label, raw, expression, dim)
    scale = TIME_UNITS[model.time_unit] ** dim[0] * LENGTH_UNITS[model.length_unit] ** dim[1]
    numbers = [q.value / scale for q in expression.args]
    if expression.kind == "constant":
        return numbers[0]
    return f"{expression.kind}({', '.join(_number_text(n) for n in numbers)})"


def strict(op: str | None = None, extra: dict[str, tuple[int, int]] | None = None) -> Callable:
    """Decorator for a tool wrapper: convert the unit arguments the caller passed.

    `op` names the create_* step whose settings in SETTINGS are converted;
    `extra` adds more arguments, e.g. {"until": time}. Only arguments the caller
    actually passed are converted, so defaults stay as they are. For a create_*
    step the text as written is kept on the model under `written[id]`. A run
    tool's result gets `time_unit` when the model has strict_units on.
    """
    settings = dict(SETTINGS.get(op, {})) if op else {}
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

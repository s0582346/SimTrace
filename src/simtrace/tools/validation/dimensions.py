"""verify_dimensions: the dimension test between the data sheet and the model.

The agent passes the model's time and length units and each figure of the data
sheet with its unit, as the sheet writes it ("12 /h", "uniform(120 s, 180 s)",
"0.1 m/s"). Each figure is converted to the model's units and compared with the
value in the build log. Parts are counted, not measured: words like parts,
items or pallets carry no dimension, so "12 parts/h" is 12 /h.

A figure in [1/time] for a time field is a rate. It is inverted for a constant
in any time field, and for exp in inter_arrival_time, time_to_failure and
repair_time. For uniform and normal, 1/X is not the same distribution, so a
rate range is rejected.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.distributions import spec_parts

_ERROR = "error"
_HINT = "hint"
_INFO = "info"

# Figures closer than this share of the larger one count as equal.
TOLERANCE = 0.01

# Dimension = (exponent of time, exponent of length).
_TIME = (1, 0)
_LENGTH = (0, 1)
_SPEED = (-1, 1)
_RATE = (-1, 0)
_COUNT = (0, 0)

_TIME_UNITS = {"s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}
_LENGTH_UNITS = {"mm": 0.001, "cm": 0.01, "m": 1.0}

# Every word a figure may use, as (seconds, metres) factors and its dimension.
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
              "pallet", "pallets", "places", "place", "unit", "units"):
    _WORDS[_name] = (1.0, _COUNT)
_WORDS["%"] = (0.01, _COUNT)

# Unit fields per build step and the dimension each needs.
FIELDS: dict[str, dict[str, tuple[int, int]]] = {
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

# Time fields where an exp given as a rate may be inverted.
_EXP_RATE_FIELDS = {"inter_arrival_time", "time_to_failure", "repair_time"}


# --- figures -----------------------------------------------------------------

@dataclass(frozen=True)
class Quantity:
    """A value in seconds and metres with its dimension."""

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
class Scalar:
    """One number of a figure, with the number literals it was written with."""

    quantity: Quantity
    literals: tuple[float, ...]


@dataclass(frozen=True)
class Figure:
    """A parsed sheet figure: a constant, a distribution or a list of figures."""

    kind: str  # "constant", "uniform", "normal", "exp" or "list"
    args: tuple  # Scalars, or Figures for "list"


_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)"
    r"|(?P<word>[A-Za-z%]+)"
    r"|(?P<op>\*\*|[*/^()+\-·×]))"
)


# Superscript powers as sheets write them, e.g. m/s², and their ^ form.
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
            raise ValueError(f"cannot read {text[position:]!r}")
        position = match.end()
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "word" and value.lower() == "per":
            kind, value = "op", "/"
        tokens.append((kind, value))
    return tokens


class _Parser:
    """expr := term ((+|-|nothing) term)*, term := power ((*|/|nothing) power)*,
    power := unary (^ integer)?, unary := -? atom,
    atom := number | unit word | ( expr ).

    A number right after a unit word starts a new summand, so "2 h 30 min"
    is 2 h + 30 min."""

    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text)
        self.at = 0
        self.literals: list[float] = []

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.tokens[self.at]
        self.at += 1
        return token

    def parse(self) -> Scalar:
        if not self.tokens:
            raise ValueError("empty figure")
        quantity = self.expr()
        if self.peek() is not None:
            raise ValueError(f"unexpected {self.peek()[1]!r}")
        return Scalar(quantity, tuple(self.literals))

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
                result = result / self.power()
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
            raise ValueError("figure ends too early")
        kind, text = self.take()
        if kind == "num":
            self.literals.append(float(text))
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


def parse_figure(text: object) -> Figure:
    """Read a sheet figure. Raises ValueError with the reason."""
    # A plain number is read as text, so 5 becomes "5".
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        text = repr(text)
    if not isinstance(text, str):
        raise ValueError("a figure must be a string such as '5 min'")
    stripped = text.strip()

    # A list such as calendar windows: each item is a figure of its own.
    if stripped.startswith("["):
        if not stripped.endswith("]"):
            raise ValueError("missing ]")
        inner = stripped[1:-1].strip()
        items = _split_top(inner) if inner else []
        return Figure("list", tuple(parse_figure(item) for item in items))

    # A distribution: each argument is read as a quantity with its unit.
    match = _DIST.match(stripped)
    if match is not None:
        name = match.group(1).lower()
        name = "normal" if name == "gauss" else name
        args = _split_top(match.group(2))
        if len(args) != _ARG_COUNT[name]:
            raise ValueError(f"{name}(...) takes {_ARG_COUNT[name]} argument(s)")
        return Figure(name, tuple(_Parser(arg).parse() for arg in args))

    # Anything else is one constant quantity.
    return Figure("constant", (_Parser(stripped).parse(),))


def _model_figure(value: object) -> tuple[str, tuple[float, ...]] | list | None:
    """The build log's value as (kind, numbers), a list of those, or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return ("constant", (float(value),))
    if isinstance(value, (list, tuple)):
        return [_model_figure(item) for item in value]
    parts = spec_parts(value)
    if parts is None:
        return None
    name, a, b = parts
    name = "normal" if name == "gauss" else name
    return (name, (a,) if b is None else (a, b))


# --- units -------------------------------------------------------------------

def dim_name(dim: tuple[int, int]) -> str:
    names = {_TIME: "[time]", _LENGTH: "[length]", _SPEED: "[length/time]",
             _RATE: "[1/time]", _COUNT: "[none]"}
    return names.get(dim, f"[time^{dim[0]} length^{dim[1]}]")


def unit_name(dim: tuple[int, int], time_unit: str, length_unit: str) -> str:
    """The unit of `dim` in the given units, e.g. 'm/min'."""
    top, bottom = [], []
    for unit, exponent in ((length_unit, dim[1]), (time_unit, dim[0])):
        if exponent == 0:
            continue
        text = unit if abs(exponent) == 1 else f"{unit}^{abs(exponent)}"
        (top if exponent > 0 else bottom).append(text)
    head = "·".join(top) or ("1" if bottom else "")
    return head + ("/" + "/".join(bottom) if bottom else "")


def _scale(dim: tuple[int, int], time_unit: str, length_unit: str) -> float:
    """Seconds and metres per unit of `dim` in the given units."""
    return _TIME_UNITS[time_unit] ** dim[0] * _LENGTH_UNITS[length_unit] ** dim[1]


def _close(a: float, b: float) -> bool:
    if abs(a) < 1e-12 and abs(b) < 1e-12:
        return True
    return abs(a - b) <= TOLERANCE * max(abs(a), abs(b))


def _all_close(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))


def _mean(kind: str, numbers: tuple[float, ...]) -> float:
    return (numbers[0] + numbers[1]) / 2 if kind == "uniform" else numbers[0]


def _show(kind: str, numbers: tuple[float, ...]) -> str:
    text = ", ".join(f"{n:.4g}" for n in numbers)
    return text if kind == "constant" else f"{kind}({text})"


# --- one entry ---------------------------------------------------------------

def _entry_label(entry: dict) -> str:
    return f"{entry.get('component')}.{entry.get('field')}"


def _finding(check: str, severity: str, component: str | None, message: str,
             details: dict | None = None) -> dict:
    finding = {"check": check, "severity": severity, "component": component,
               "message": message}
    if details is not None:
        finding["details"] = details
    return finding


def _causes(figure: Figure, expected: tuple[float, ...], model: tuple[float, ...],
            dim: tuple[int, int], rate: bool, units: tuple[str, str]) -> str:
    """The known slip whose value the model holds, or 'no known cause'."""
    time_unit, length_unit = units
    scalars = figure.args

    # The number as the sheet writes it. Only for arguments with one number
    # in them: "120 s" has one, "5 * 8 h" has two.
    written = tuple(s.literals[0] if len(s.literals) == 1 else None for s in scalars)
    if None not in written and _all_close(written, model):
        return ("sheet figure copied without inverting or converting" if rate
                else "sheet figure copied without converting")

    # A rate: the rate itself in the model's unit, or the written number inverted.
    if rate:
        per_unit = tuple(s.quantity.value * _TIME_UNITS[time_unit] for s in scalars)
        if _all_close(per_unit, model):
            return f"rate entered instead of the mean (per {time_unit})"
        if None not in written and all(w != 0 for w in written) and _all_close(
            tuple(1 / w for w in written), model
        ):
            return "inverted but not converted"

    # The expected value in every other pair of time and length units. Units
    # of a dimension the field does not have are left at the model's.
    in_model = _scale(dim, time_unit, length_unit)
    for alt_time in _TIME_UNITS:
        for alt_length in _LENGTH_UNITS:
            if dim[0] == 0 and alt_time != time_unit:
                continue
            if dim[1] == 0 and alt_length != length_unit:
                continue
            if (alt_time, alt_length) == (time_unit, length_unit):
                continue
            factor = in_model / _scale(dim, alt_time, alt_length)
            if _all_close(tuple(e * factor for e in expected), model):
                return (f"value in {unit_name(dim, alt_time, alt_length)} instead "
                        f"of {unit_name(dim, time_unit, length_unit)}")
    return "no known cause"


def _check_scalar_figure(entry: dict, figure: Figure, model_value: object,
                         dim: tuple[int, int], units: tuple[str, str]) -> tuple[dict, list[dict]]:
    """Compare one constant or distribution figure with the model's value."""
    label = _entry_label(entry)
    component, field = entry["component"], entry["field"]
    sheet = entry["sheet"]

    # 1. The sheet figure's dimension. A bare 0 is 0 in every unit, so it
    # takes the dimension of the rest.
    dims = {s.quantity.dim for s in figure.args
            if not (s.quantity.value == 0 and s.quantity.dim == _COUNT)} or {dim}
    if len(dims) != 1:
        raise _Rejected("invalid_figure", f"{label}: sheet {sheet} mixes dimensions.")
    sheet_dim = dims.pop()

    # 2. Same dimension as the field, or a rate for a time field. Anything
    # else has no conversion. A rate is inverted only for a constant, or for
    # exp in the fields of _EXP_RATE_FIELDS.
    rate = sheet_dim != dim and dim == _TIME and sheet_dim == _RATE
    if sheet_dim != dim and not rate:
        raise _Rejected(
            "incompatible_dimension",
            f"{label}: sheet {sheet} is {dim_name(sheet_dim)}, the field needs "
            f"{dim_name(dim)}. There is no conversion.",
        )
    if rate:
        if figure.kind not in ("constant", "exp"):
            raise _Rejected(
                "not_invertible",
                f"{label}: sheet {sheet} is a rate range. 1/X of {figure.kind} is "
                "not the same distribution, so it cannot be inverted. Give the "
                "times instead.",
            )
        if figure.kind == "exp" and field not in _EXP_RATE_FIELDS:
            raise _Rejected(
                "not_invertible",
                f"{label}: an exp given as a rate is inverted only in "
                f"{', '.join(sorted(_EXP_RATE_FIELDS))}.",
            )
        if any(s.quantity.value == 0 for s in figure.args):
            raise _Rejected("invalid_figure", f"{label}: a rate of 0 has no mean.")

    # 3. The expected value in the model's units.
    time_unit, length_unit = units
    scale = _scale(dim, time_unit, length_unit)
    if rate:
        expected = tuple(1 / s.quantity.value / scale for s in figure.args)
    else:
        expected = tuple(s.quantity.value / scale for s in figure.args)
    expected_kind = figure.kind

    # 4. The value in the build log.
    if model_value is None:
        raise _Rejected("invalid_figure", f"{label}: the model has no value for this field.")
    parsed = _model_figure(model_value)
    if parsed is None or isinstance(parsed, list):
        raise _Rejected("invalid_figure",
                        f"{label}: the model value {model_value!r} is not a number.")
    model_kind, model_numbers = parsed

    # 5. Compare argument by argument for the same kind, else the means.
    if model_kind == expected_kind:
        expected_cmp, model_cmp = expected, model_numbers
    else:
        # A sheet that gives one figure for a distribution gives its mean.
        expected_cmp = (_mean(expected_kind, expected),)
        model_cmp = (_mean(model_kind, model_numbers),)
    unit = unit_name(dim, time_unit, length_unit)
    row = {
        "component": component,
        "field": field,
        "sheet": sheet,
        "expected": _show(expected_kind, expected) + (f" {unit}" if unit else ""),
        "model": model_value,
    }
    if _all_close(expected_cmp, model_cmp):
        row["status"] = "pass"
        findings = []
        if rate:
            row["status"] = "info"
            findings.append(_finding(
                "converted_from_rate", _INFO, component,
                f"{label}: sheet {sheet}, inverted {row['expected']}, model "
                f"{_show(model_kind, model_numbers)}.",
            ))
        return row, findings

    # 6. A mismatch: a wrong count, or a unit slip with its likely cause.
    row["status"] = "error"
    if dim == _COUNT:
        # A count has no unit, so a wrong count is a wrong value, not a unit slip.
        return row, [_finding(
            "value_mismatch", _ERROR, component,
            f"{label}: sheet {sheet}, expected {row['expected']}, model "
            f"{_show(model_kind, model_numbers)}.",
        )]
    cause = _causes(
        figure if model_kind == expected_kind else Figure("constant", (figure.args[0],)),
        expected_cmp, model_cmp, dim, rate, units,
    )
    check = "wrong_dimension" if rate else "unit_mismatch"
    middle = f"inverted {row['expected']}" if rate else f"expected {row['expected']}"
    message = (f"{label}: sheet {sheet}, {middle}, model "
               f"{_show(model_kind, model_numbers)}. Likely cause: {cause}.")
    if entry.get("_op") == "create_conveyor" and field == "speed":
        message += _travel_note(entry, expected_cmp[0], model_cmp[0], units)
    return row, [_finding(check, _ERROR, component, message, {"likely_cause": cause})]


def _travel_note(entry: dict, expected_speed: float, model_speed: float,
                 units: tuple[str, str]) -> str:
    length = entry.get("_kwargs", {}).get("conveyor_length")
    if not isinstance(length, (int, float)) or expected_speed <= 0 or model_speed <= 0:
        return ""
    return (f" Travel time {length / expected_speed:.4g} {units[0]} expected, "
            f"{length / model_speed:.4g} in the model.")


class _Rejected(Exception):
    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check
        self.message = message


def _check_list(entry: dict, figure: Figure, model_value: object,
                dim: tuple[int, int], units: tuple[str, str]) -> tuple[dict, list[dict]]:
    """Calendar windows: a list of [start, end] pairs, compared number by number."""
    label = _entry_label(entry)

    def flatten(fig: Figure) -> list[Figure]:
        if fig.kind == "list":
            return [leaf for item in fig.args for leaf in flatten(item)]
        return [fig]

    def flatten_model(value: object) -> list:
        if isinstance(value, (list, tuple)):
            return [leaf for item in value for leaf in flatten_model(item)]
        return [value]

    # Both sides as flat lists of numbers: [[0, 8], [9, 16]] -> [0, 8, 9, 16].
    sheet_leaves, model_leaves = flatten(figure), flatten_model(model_value)
    if len(sheet_leaves) != len(model_leaves):
        raise _Rejected(
            "invalid_figure",
            f"{label}: sheet {entry['sheet']} has {len(sheet_leaves)} numbers, "
            f"the model {len(model_leaves)}.",
        )
    # Each number is checked like a single figure.
    findings: list[dict] = []
    status = "pass"
    for leaf, value in zip(sheet_leaves, model_leaves):
        if leaf.kind != "constant":
            raise _Rejected("invalid_figure", f"{label}: windows hold plain times.")
        _, found = _check_scalar_figure(entry, leaf, value, dim, units)
        if any(f["severity"] == _ERROR for f in found):
            # Stops at the first wrong number. Further errors in the list
            # stay unreported.
            status = "error"
            findings = found
            break
        if found:
            status = "info"
            findings += found
    row = {"component": entry["component"], "field": entry["field"],
           "sheet": entry["sheet"], "model": model_value, "status": status}
    return row, findings


# --- the tool ----------------------------------------------------------------

def _components(model: FactoryModel) -> dict[str, tuple[str, dict]]:
    """Component id -> (build op, recorded kwargs) for every create_* step."""
    found: dict[str, tuple[str, dict]] = {}
    for step in model.spec:
        kwargs = step["kwargs"]
        if step["op"] in FIELDS and "id" in kwargs:
            found[kwargs["id"]] = (step["op"], kwargs)
    return found


def _closest(name: str, options) -> str | None:
    match = difflib.get_close_matches(str(name), list(options), n=1, cutoff=0.6)
    return match[0] if match else None


def _undeclared(components: dict, declared: set) -> list[dict]:
    """Time and length fields with a value but no sheet figure."""
    has_conveyor = any(op == "create_conveyor" for op, _ in components.values())
    findings = []
    for component, (op, kwargs) in components.items():
        for field, dim in FIELDS[op].items():
            # Counts and fields with a sheet figure are left out.
            if dim == _COUNT or (component, field) in declared:
                continue
            # A source's item length is used only on a conveyor.
            if op == "create_source" and field == "item_length" and not has_conveyor:
                continue
            # No value, or 0, needs no unit.
            value = kwargs.get(field)
            if value is None or isinstance(value, bool) or value == 0 or value == []:
                continue
            findings.append(_finding(
                "undeclared", _HINT, component,
                f"{component}.{field} has a value ({value!r}) but no figure from "
                "the sheet.",
            ))
    return findings


def verify_dimensions(
    time_unit: str,
    figures: list[dict],
    length_unit: str = "m",
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Compare every sheet figure, converted to the model's units, with the model.

    Args:
        time_unit: the model's time unit: s, min, h or d.
        figures: one dict per sheet figure with `component`, `field` and
            `sheet`, the figure as the sheet writes it, e.g. "12 /h",
            "uniform(120 s, 180 s)", "0.1 m/s", "[[0 h, 8 h]]".
        length_unit: the model's length unit: mm, cm or m.

    Returns `passed`, `summary`, `units`, `checked` (one row per figure),
    and `findings`.
    """
    # Arguments that cannot be checked at all raise, like the other tools.
    model = model if model is not None else get_session_model()
    if time_unit not in _TIME_UNITS:
        raise ValueError(f"time_unit must be one of {', '.join(_TIME_UNITS)} (got {time_unit!r}).")
    if length_unit not in _LENGTH_UNITS:
        raise ValueError(
            f"length_unit must be one of {', '.join(_LENGTH_UNITS)} (got {length_unit!r})."
        )
    if not isinstance(figures, list):
        raise ValueError("figures must be a list of {component, field, sheet} dicts.")
    components = _components(model)
    if not components:
        raise ValueError("The model is empty. Build it before checking its units.")
    units = (time_unit, length_unit)

    # Each figure: find its component and field, read it, compare it. An
    # entry that cannot be checked becomes a finding and the next one follows.
    findings: list[dict] = []
    checked: list[dict] = []
    declared: set = set()
    for entry in figures:
        if not isinstance(entry, dict) or not {"component", "field", "sheet"} <= entry.keys():
            raise ValueError("each figure needs component, field and sheet.")
        component, field = entry["component"], entry["field"]
        label = _entry_label(entry)
        try:
            if component not in components:
                closest = _closest(component, components)
                raise _Rejected(
                    "unknown_entry",
                    f"{label}: no component {component} in the model."
                    + (f" Closest: {closest}." if closest else ""),
                )
            op, kwargs = components[component]
            if field not in FIELDS[op]:
                closest = _closest(field, FIELDS[op])
                raise _Rejected(
                    "unknown_entry",
                    f"{label}: {field} is not a unit field of {op}."
                    + (f" Closest: {closest}." if closest else ""),
                )
            declared.add((component, field))
            try:
                figure = parse_figure(entry["sheet"])
            except ValueError as exc:
                raise _Rejected("invalid_figure", f"{label}: sheet {entry['sheet']!r}: {exc}.")
            work = dict(entry, _op=op, _kwargs=kwargs)
            dim = FIELDS[op][field]
            if figure.kind == "list":
                row, found = _check_list(work, figure, kwargs.get(field), dim, units)
            else:
                row, found = _check_scalar_figure(work, figure, kwargs.get(field), dim, units)
            checked.append(row)
            findings += found
        except _Rejected as rejected:
            checked.append({"component": component, "field": field,
                            "sheet": entry["sheet"], "status": "rejected"})
            findings.append(_finding(rejected.check, _ERROR, component, rejected.message))

    # Model values the sheet list left out.
    findings += _undeclared(components, declared)

    # Summary line and result.
    errors = [f for f in findings if f["severity"] == _ERROR]
    hints = [f for f in findings if f["severity"] == _HINT]
    passed_rows = sum(1 for row in checked if row["status"] in ("pass", "info"))
    summary = (f"{passed_rows} of {len(checked)} figures match the model, "
               f"{len(errors)} error(s), {len(hints)} hint(s).")
    return {
        "passed": not errors,
        "summary": summary,
        "units": {"time": time_unit, "length": length_unit},
        "checked": checked,
        "findings": findings,
    }

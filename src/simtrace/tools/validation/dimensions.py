"""verify_dimensions: the dimension test after Rabe et al. between the data sheet and the model.

The agent passes each figure of the data sheet with its unit, as the sheet
writes it ("12 /h", "uniform(120 s, 180 s)", "0.1 m/s"), and the model field it
went into. The tool reads the figure's dimension and checks it against the
dimension the field needs. It does not compare values and needs no model unit.
Parts are counted, not measured: words like parts, items or pallets carry no
dimension, so "12 parts/h" is [1/time].

A figure in [1/time] for a time field is a rate. It fits after inverting: a
constant in any time field, exp in inter_arrival_time, time_to_failure and
repair_time. For uniform and normal, 1/X is not the same distribution, so a
rate range is rejected.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.distributions import unquote

_ERROR = "error"
_INFO = "info"

# Dimension = (exponent of time, exponent of length).
_TIME = (1, 0)
_LENGTH = (0, 1)
_SPEED = (-1, 1)
_RATE = (-1, 0)
_COUNT = (0, 0)

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
              "pallet", "pallets", "places", "place", "unit", "units",
              "failure", "failures", "breakdown", "breakdowns", "order", "orders",
              "housing", "housings", "shaft", "shafts", "box", "boxes",
              "carton", "cartons", "crate", "crates", "package", "packages",
              "bag", "bags", "batch", "batches", "lot", "lots", "job", "jobs",
              "trip", "trips"):
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
    stripped = unquote(text)

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


# --- dimensions --------------------------------------------------------------

def dim_name(dim: tuple[int, int]) -> str:
    names = {_TIME: "[time]", _LENGTH: "[length]", _SPEED: "[length/time]",
             _RATE: "[1/time]", _COUNT: "[none]"}
    return names.get(dim, f"[time^{dim[0]} length^{dim[1]}]")


def _finding(check: str, severity: str, component: str | None, message: str) -> dict:
    return {"check": check, "severity": severity, "component": component,
            "message": message}


class _Rejected(Exception):
    def __init__(self, check: str, message: str) -> None:
        super().__init__(message)
        self.check = check
        self.message = message


def _entry_label(entry: dict) -> str:
    return f"{entry.get('component')}.{entry.get('field')}"


def _figure_dim(label: str, sheet: str, figure: Figure, dim: tuple[int, int]) -> tuple[int, int]:
    """The one dimension of a constant or distribution figure. A bare 0 is 0
    in every unit, so it takes the dimension of the rest."""
    dims = {s.quantity.dim for s in figure.args
            if not (s.quantity.value == 0 and s.quantity.dim == _COUNT)} or {dim}
    if len(dims) != 1:
        raise _Rejected("invalid_figure", f"{label}: sheet {sheet} mixes dimensions.")
    return dims.pop()


def _check_scalar_figure(entry: dict, figure: Figure,
                         dim: tuple[int, int]) -> tuple[str, list[dict]]:
    """Status and findings for one constant or distribution figure."""
    label = _entry_label(entry)
    component, field, sheet = entry["component"], entry["field"], entry["sheet"]
    sheet_dim = _figure_dim(label, sheet, figure, dim)
    if sheet_dim == dim:
        return "pass", []

    # A rate for a time field fits after inverting. Anything else does not fit.
    if not (dim == _TIME and sheet_dim == _RATE):
        raise _Rejected(
            "incompatible_dimension",
            f"{label}: sheet {sheet} is {dim_name(sheet_dim)}, the field needs "
            f"{dim_name(dim)}.",
        )
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
    return "info", [_finding(
        "needs_inverting", _INFO, component,
        f"{label}: sheet {sheet} is [1/time], the field needs [time]. The model "
        "value must be the inverted mean.",
    )]


def _check_list(entry: dict, figure: Figure, dim: tuple[int, int]) -> tuple[str, list[dict]]:
    """Calendar windows: every number in the list is a plain time."""
    label = _entry_label(entry)

    def flatten(fig: Figure) -> list[Figure]:
        if fig.kind == "list":
            return [leaf for item in fig.args for leaf in flatten(item)]
        return [fig]

    status, findings = "pass", []
    for leaf in flatten(figure):
        if leaf.kind != "constant":
            raise _Rejected("invalid_figure", f"{label}: windows hold plain times.")
        leaf_status, found = _check_scalar_figure(entry, leaf, dim)
        if leaf_status == "info":
            status = "info"
            findings += found
    return status, findings


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
                "undeclared", _ERROR, component,
                f"{component}.{field} has a value ({value!r}) but was not passed. "
                "Pass the sheet's value with its unit, or the calculation from "
                "the sheet's values if you worked it out.",
            ))
    return findings


def verify_dimensions(figures: list[dict], *, model: FactoryModel | None = None) -> dict:
    """Check that every sheet figure has the dimension of the model field it went into.

    Args:
        figures: one dict per sheet figure with `component`, `field` and
            `sheet`, the figure as the sheet writes it, e.g. "12 /h",
            "uniform(120 s, 180 s)", "0.1 m/s", "[[0 h, 8 h]]".

    Returns `passed`, `summary`, `checked` (one row per figure) and `findings`.
    """
    # Arguments that cannot be checked at all raise, like the other tools.
    model = model if model is not None else get_session_model()
    if not isinstance(figures, list):
        raise ValueError("figures must be a list of {component, field, sheet} dicts.")
    components = _components(model)
    if not components:
        raise ValueError("The model is empty. Build it before checking its dimensions.")

    # Each figure: find its component and field, read it, check its dimension.
    # An entry that cannot be checked becomes a finding and the next one follows.
    findings: list[dict] = []
    checked: list[dict] = []
    declared: set = set()
    for entry in figures:
        if not isinstance(entry, dict) or not {"component", "field", "sheet"} <= entry.keys():
            raise ValueError("each figure needs component, field and sheet.")
        component, field = entry["component"], entry["field"]
        label = _entry_label(entry)
        row = {"component": component, "field": field, "sheet": entry["sheet"]}
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
            if kwargs.get(field) is None:
                raise _Rejected("invalid_figure",
                                f"{label}: the model has no value for this field.")
            try:
                figure = parse_figure(entry["sheet"])
            except ValueError as exc:
                raise _Rejected("invalid_figure", f"{label}: sheet {entry['sheet']!r}: {exc}.")
            dim = FIELDS[op][field]
            row["needs"] = dim_name(dim)
            if figure.kind == "list":
                row["status"], found = _check_list(entry, figure, dim)
            else:
                row["status"], found = _check_scalar_figure(entry, figure, dim)
            findings += found
        except _Rejected as rejected:
            row["status"] = "rejected"
            findings.append(_finding(rejected.check, _ERROR, component, rejected.message))
        checked.append(row)

    # Model values the sheet list left out.
    findings += _undeclared(components, declared)

    # Summary line and result.
    errors = [f for f in findings if f["severity"] == _ERROR]
    fitting = sum(1 for row in checked if row["status"] in ("pass", "info"))
    summary = (f"{fitting} of {len(checked)} figures have the dimension of their "
               f"field, {len(errors)} error(s).")
    return {"passed": not errors, "summary": summary, "checked": checked,
            "findings": findings}

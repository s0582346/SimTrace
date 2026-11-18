# Dimension test

## What the test is

Rabe, Spieckermann and Wenzel describe the dimension test (Dimensionstest) as
unit algebra on formulas. Every variable in a formula is replaced by its unit
(m, s, m/s, parts/h), and the units are simplified. Every sum and difference
must have one unit, and the result must have the unit of the target variable.

Their example is the acceleration distance of a vehicle,
s = (v_max² − v₀²) / (2b), which gives [s²/m] · [m²/s²] = [m]. If v₀ is not
squared, the bracket holds m²/s² − m/s, and the formula is wrong.

simtrace applies it in two places:

1. **The arguments the agent passes** to the create_* tools and to the run
   tools. This check runs in each call, when strict units are on.
2. **The formulas in the code** (FactorySimPy and the report). They are fixed
   code that an LLM cannot change, so they are checked by unit tests and never
   at run time.

## 1. Arguments: strict units

### Switching it on

The server reads the environment variables once, when it starts
(`server._units_from_env`):

| Variable | Values | Default |
|---|---|---|
| SIMTRACE_STRICT_UNITS | 1, true, yes or on | off |
| SIMTRACE_TIME_UNIT | s, min, h, d | min |
| SIMTRACE_LENGTH_UNIT | mm, cm, m | m |

With strict units off, every argument passes through unchanged, bare numbers
are allowed and no dimension is checked.

### What the agent sees

With strict units on:

- the server instructions get a units block before "Modeling patterns",
- every create_* tool and every run tool gets a units sentence at the end of
  its description,
- `reset_model` returns the model's time and length unit,
- the run tools return `time_unit` with their result.

### What the check does

Every time, length and speed argument, and every `until`, must be written with
its unit, e.g. `"uniform(3 min, 4 min)"`, `"0.3 m"`, `"20 parts/min * 0.3 m"`,
`"1 / (12 parts/h)"`, `"8 h"`. For each one the call:

1. reads the text into a number in seconds and metres, with its dimension,
2. compares the dimension with the one the setting needs,
3. refuses the call if they differ, before anything is built,
4. otherwise converts the number into the model's units and passes it on.

The dimension is written as (exponent of time, exponent of length):

| Dimension | Settings |
|---|---|
| time (1, 0) | inter_arrival_time, processing_delay, node_setup_time, time_to_failure, repair_time, buffer and fleet delay, transit_delay, calendar cycle and windows, until |
| length (0, 1) | conveyor_length, item_length |
| length/time (−1, 1) | speed |
| none (0, 0) | capacity, work_capacity, split_quantity, route shares |

Settings without a dimension are plain numbers and are not checked.

Rules:

- Parts are counted, not measured. Words like parts, items, boxes or pallets
  carry no dimension, so "12 parts/h" is [1/time].
- A rate is not a time. "exp(12 parts/h)" for an arrival time is refused. The
  inversion "exp(1 / (12 parts/h))" is a time and is accepted.
- A bare number other than 0 is refused with "has no unit". 0 is 0 in every
  unit and is accepted.
- Each argument of a distribution is read on its own; all must have the
  dimension of the setting.
- A refusal names the setting, the dimension given and the dimension needed,
  e.g. `belt.speed: 20 parts/min is [1/time], the setting needs [length/time].`
  It does not give the right value.

The text as written is kept in `model.written[id]`, so a reader can see where
each number came from.

### How it is implemented

`src/simtrace/tools/units.py`:

- `SETTINGS` maps each create_* tool to its unit settings and their dimension.
  `UNTIL` does the same for the run tools.
- `read(text)` parses a constant or a distribution (uniform, normal, gauss,
  exp). The parser knows the time and length units, count words, `*`, `/`,
  `per`, `^`, brackets and superscripts. "2 h 30 min" is a sum, "3 per 8 h" is
  3 / (8 h), "6 m / 1 min" is 6 m / (1 min). A sum of two dimensions cannot be
  read.
- `convert(label, raw, dim, model)` checks the dimension and converts.
- `strict(op, extra)` is the decorator on each tool wrapper in
  `registry.py`. It converts only the arguments the caller passed, so
  defaults stay as they are.

The builders and the build log get plain numbers in the model's units, as
before.

### What it cannot catch

A wrong number with the right dimension: 6000 instead of 6 m/min, seconds typed
as minutes, or uniform(30 min, 40 min) instead of uniform(3 min, 4 min). The
test checks dimensions, not numbers.

### Tests

`tests/test_tools/test_units.py` plants an argument of each wrong dimension in a
time, a length and a speed setting and in `until`, and checks that the call is
refused, nothing is built, and the right argument builds. It also covers the
parser, the conversion, the server settings and the tool descriptions.

## 2. Formulas in the code

`tests/test_tools/test_formula_dimensions.py` writes the inputs in another unit
and checks two rules:

1. A result with a dimension scales the way its dimension says, and a result
   without one does not change.
2. A non-linear function (int, ceil, floor, round, log, exp) is applied only to
   a dimensionless argument.

Covered formulas:

| Formula | Dimension |
|---|---|
| places on a belt = floor(belt length / part length) | none, same in m, cm and mm |
| travel time on a belt = belt length / speed | time |
| throughput per hour in the report | 1/time, same for every model time unit |

## Study

The agent study, its results and why a separate verify_dimensions tool was
dropped are in `benchmarks/agent_study/dimensions/README.md`.

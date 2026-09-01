"""Show what the replication search does, run by run.

`find_replication_count` returns a count. This prints the evidence behind it:
for every replication, the running mean, the confidence interval around it, and
the interval's half-width as a share of that mean. Reading down that last column
is the whole argument - the share falls as runs accumulate, crosses the target,
and then has to stay across for the look-ahead window before the count is
accepted.

A tighter target needs more runs, so the default here is 5% rather than the 10%
`find_replication_count` uses. That makes the table long enough to show the
crossing, the window, and the settling, instead of just the crossing.

Run it:

    uv run python benchmarks/replication_search.py
    uv run python benchmarks/replication_search.py --precision 0.10 0.05 0.02
    uv run python benchmarks/replication_search.py --plot out/search.html

The seed is fixed, so the numbers are the same on every machine and can be
quoted directly.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List

from simtrace.model import FactoryModel
from simtrace.tools.builders import create_buffer, create_sink, create_source
from simtrace.tools.simulation import connect
from simtrace.tools.simulation.adaptive import find_replication_count

# The metric the search converges on: items that reached the end of the line.
THROUGHPUT = "snk.num_item_received"


def build_line() -> FactoryModel:
    """A one-line model with random arrivals, so the runs actually differ."""
    model = FactoryModel()
    create_source("src", inter_arrival_time="exp(1)", blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("buf", capacity=4, model=model)
    connect("buf", "src", "snk", model=model)
    return model


def print_table(rows: List[Dict[str, Any]], target: float, needed: int | None) -> None:
    """One line per replication, with the run that settles it marked."""
    print(f"{'run':>4}  {'result':>8}  {'mean so far':>12}  "
          f"{'+/- share of mean':>18}  {'within target':>14}")

    for row in rows:
        share = row["% deviation"]
        if share is None:
            # No standard deviation exists on the first runs, so no interval.
            share_text, verdict = "-", "-"
        else:
            share_text = f"{share:.2%}"
            verdict = "yes" if share <= target else "no"

        mark = "  <- settles here" if row["replications"] == needed else ""
        print(
            f"{row['replications']:>4}  {row['Mean']:>8.1f}  "
            f"{row['Cumulative Mean']:>12.2f}  {share_text:>18}  "
            f"{verdict:>14}{mark}"
        )


def _plot_path(path: str | None, target: float, several: bool) -> str | None:
    """Keep one target's figure from overwriting the next one's."""
    if path is None or not several:
        return path

    stem, extension = os.path.splitext(path)
    return f"{stem}.p{round(target * 100)}{extension or '.html'}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--precision",
        type=float,
        nargs="+",
        default=[0.05],
        help="target share(s) of the mean; one table per target (default: 0.05)",
    )
    parser.add_argument(
        "--until", type=float, default=200, help="simulated time per run"
    )
    parser.add_argument(
        "--budget", type=int, default=100, help="where the search gives up"
    )
    parser.add_argument(
        "--look-ahead",
        type=int,
        default=5,
        help="runs the target must hold for before a count is accepted",
    )
    parser.add_argument("--seed", type=int, default=0, help="base RNG seed")
    parser.add_argument(
        "--plot",
        default=None,
        help="write the figure here (HTML); omitted, nothing is written",
    )
    args = parser.parse_args()

    for target in args.precision:
        result = find_replication_count(
            until=args.until,
            desired_precision=target,
            look_ahead=args.look_ahead,
            replication_budget=args.budget,
            random_seed_base=args.seed,
            model=build_line(),
            plot_path=_plot_path(args.plot, target, len(args.precision) > 1),
        )
        needed = result["replications_needed"][THROUGHPUT]

        print()
        print(f"target: within +/-{target:.0%} of the mean, "
              f"95% confidence, look-ahead {args.look_ahead}")
        print("-" * 70)
        print_table(result["tables"][THROUGHPUT], target, needed)
        print("-" * 70)

        if needed is None:
            print(
                f"never settled within {args.budget} runs "
                f"({result['runs_executed']} run)"
            )
        else:
            print(
                f"replications needed: {needed}   "
                f"(runs made: {result['runs_executed']} - the look-ahead window "
                f"and the batching both run past the count)"
            )
        for path in result["plot_paths"].values():
            print(f"figure: {path}")


if __name__ == "__main__":
    main()

"""Draw a precision search: the running mean, its interval, and where it settled.

The figure is `sim_tools.output_analysis.plotly_confidence_interval_method` — a
cumulative mean line with a shaded confidence band that narrows as replications
accumulate, and a dashed marker at the count the search picked. Reading it is
the quickest way to tell a model that genuinely settles from one that happened
to dip below the target for a run or two.

The input is a table of row dicts as `find_replication_count` returns them, so a
caller can plot a search it has already stored, without re-running anything.

HTML, not an image: a static export needs `kaleido`, which is not a dependency.
The plotly runtime is embedded in the file, so it opens offline at the cost of a
few megabytes.

`sim_tools` and `pandas` are imported inside the function, as everywhere else
here — a worker process should not pay for plotting it will never do.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Sequence

import numpy as np

# What sim-tools' plotting function reads off the frame.
REQUIRED_COLUMNS = ("Cumulative Mean", "Lower Interval", "Upper Interval")


def _safe(metric: str) -> str:
    """A metric name reduced to something safe in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", metric)


def _target_path(path: str, metric: str, suffix_with_metric: bool) -> str:
    """Where this metric's figure goes.

    One metric writes to `path` as given. Several would overwrite each other, so
    the metric name goes in front of the extension: `search.html` becomes
    `search.snk.num_item_received.html`.
    """
    if not suffix_with_metric:
        return os.path.abspath(path)

    stem, extension = os.path.splitext(path)
    return os.path.abspath(f"{stem}.{_safe(metric)}{extension or '.html'}")


def write_precision_plot(
    metric: str,
    replications_needed: int | None,
    table: Sequence[Dict[str, Any]],
    path: str,
    *,
    suffix_with_metric: bool = False,
) -> str:
    """Write one metric's precision plot and return the file's absolute path.

    Args:
        metric: the metric's name, used on the y axis and in the filename.
        replications_needed: the count to mark. None — the target was never
            held — draws the curve with no marker, which is the honest picture.
        table: row dicts with `replications` and the columns in
            `REQUIRED_COLUMNS`. None values come back as gaps in the line, which
            is what the first runs hold before an interval exists.
        path: the file to write. Missing parent directories are created.
        suffix_with_metric: put the metric name in the filename, for a search
            over several metrics.

    Returns:
        The absolute path written.

    Raises:
        ValueError: if `table` is empty or missing a column the figure needs.
    """
    if not table:
        raise ValueError(f"No replication table to plot for {metric!r}.")

    import pandas as pd
    from sim_tools.output_analysis import plotly_confidence_interval_method

    frame = pd.DataFrame(list(table))
    missing = [column for column in REQUIRED_COLUMNS if column not in frame]
    if missing:
        raise ValueError(
            f"The table for {metric!r} has no {missing}; it does not look like "
            f"a replication table."
        )

    # The figure plots against the replication number, and None -> NaN gives the
    # gaps rather than a line starting at zero.
    frame = frame.set_index("replications")
    for column in REQUIRED_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    # sim-tools computes a percentage deviation for the hover text by dividing
    # by the cumulative mean, which is zero for a metric that never moved.
    with np.errstate(invalid="ignore", divide="ignore"):
        figure = plotly_confidence_interval_method(
            n_reps=replications_needed,
            conf_ints=frame,
            metric_name=metric,
        )

    if replications_needed is None:
        # The marker is a vertical line at n_reps; without a count there is
        # nothing to mark, and plotly would place it at the origin.
        figure.layout.shapes = ()

    target = _target_path(path, metric, suffix_with_metric)
    # A caller naming out/search.html is asking for the file, not for a
    # lecture about the folder.
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    figure.write_html(target)
    return target


def write_precision_plots(
    tables: Dict[str, List[Dict[str, Any]]],
    replications_needed: Dict[str, int | None],
    path: str,
) -> Dict[str, str]:
    """Write one figure per metric. Returns metric -> the path written."""
    return {
        metric: write_precision_plot(
            metric=metric,
            replications_needed=replications_needed.get(metric),
            table=table,
            path=path,
            suffix_with_metric=len(tables) > 1,
        )
        for metric, table in tables.items()
    }

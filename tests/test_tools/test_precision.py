"""Tests for the confidence interval method wrapper in `precision`.

The wrapper's job is translation, not statistics: sim-tools decides the run
count, and these tests hold the wrapper to sim-tools' own answer on the same
data. What is checked here is the translation — numpy scalars to native ones,
`-1` to None, `nan` to None, and a payload that survives `json.dumps`.
"""

import json
import warnings

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import create_buffer, create_sink, create_source
from simtrace.tools.simulation import connect, run_replications
from simtrace.tools.simulation.precision import (
    DEFAULT_ALPHA,
    MIN_ASSESSABLE,
    precision_report,
)
from simtrace.tools.simulation.replication_analysis import extract_metrics
from simtrace.tools.simulation.replications import sink_throughput_metrics


def _build_stochastic_line() -> FactoryModel:
    """A freshly built line whose arrivals are random draws (source->buf->sink)."""
    m = FactoryModel()
    create_source("src", inter_arrival_time="exp(1)", blocking=True, model=m)
    create_sink("snk", model=m)
    create_buffer("buf", capacity=4, model=m)
    connect("buf", "src", "snk", model=m)
    return m


# A settled series: the running mean barely moves, so the interval tightens
# quickly and the target is reached well inside the batch.
SETTLED = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2, 99.8, 100.1, 99.9, 100.3]

# A series that keeps jumping: the interval never gets near 10% of the mean.
ERRATIC = [1.0, 90.0, 5.0, 140.0, 2.0, 200.0, 8.0, 170.0, 3.0, 120.0]


# --- agreement with sim-tools ----------------------------------------------


def test_matches_sim_tools_on_the_same_data():
    """The reported count is the one sim-tools picks, not our own reading."""
    from sim_tools.output_analysis import confidence_interval_method

    expected, _ = confidence_interval_method(
        SETTLED,
        alpha=DEFAULT_ALPHA,
        desired_precision=0.10,
        min_rep=MIN_ASSESSABLE,
    )

    report = precision_report({"throughput": SETTLED})

    assert report["metrics"]["throughput"]["replications_needed"] == expected


def test_target_reached_is_reported_with_a_count():
    report = precision_report({"throughput": SETTLED})
    entry = report["metrics"]["throughput"]

    assert entry["reached"] is True
    assert MIN_ASSESSABLE < entry["replications_needed"] <= len(SETTLED)


# --- translation -----------------------------------------------------------


def test_unreached_target_becomes_none_not_minus_one():
    """sim-tools says -1 for "never reached"; that is not a run count."""
    report = precision_report({"throughput": ERRATIC})
    entry = report["metrics"]["throughput"]

    assert entry["reached"] is False
    assert entry["replications_needed"] is None


def test_no_warning_leaks_for_an_unreached_target():
    """sim-tools warns once per unreached metric; `reached` already says it."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        precision_report({"a": ERRATIC, "b": ERRATIC})

    assert [str(w.message) for w in caught] == []


def test_result_is_json_serializable():
    """numpy scalars and nan would both break the MCP channel."""
    report = precision_report({"throughput": SETTLED}, tables_for=["throughput"])

    json.dumps(report)


def test_table_rows_carry_their_replication_number():
    report = precision_report({"throughput": SETTLED}, tables_for=["throughput"])
    table = report["metrics"]["throughput"]["table"]

    assert [row["replications"] for row in table] == list(
        range(1, len(SETTLED) + 1)
    )
    # The first rows have no standard deviation yet; nan must arrive as None.
    assert table[0]["Standard Deviation"] is None
    assert table[-1]["Cumulative Mean"] is not None


def test_table_only_for_the_metrics_asked_for():
    report = precision_report(
        {"wanted": SETTLED, "other": SETTLED}, tables_for=["wanted"]
    )

    assert "table" in report["metrics"]["wanted"]
    assert "table" not in report["metrics"]["other"]


# --- guards ----------------------------------------------------------------


def test_too_few_replications_reports_a_note_not_false_negatives():
    """Nothing is left to test after the skipped runs, so nothing is reported."""
    report = precision_report({"throughput": SETTLED[:MIN_ASSESSABLE]})

    assert report["metrics"] == {}
    assert "too few" in report["note"]


def test_metric_with_a_single_value_is_skipped():
    report = precision_report({"throughput": SETTLED, "once": [1.0]})

    assert "once" not in report["metrics"]


@pytest.mark.parametrize("bad", [0, 1, 1.5, -0.1, "0.1", True])
def test_bad_desired_precision_rejected(bad):
    with pytest.raises(ValueError, match="desired_precision"):
        precision_report({"throughput": SETTLED}, desired_precision=bad)


def test_bad_min_replications_rejected():
    with pytest.raises(ValueError, match="min_replications"):
        precision_report({"throughput": SETTLED}, min_replications=-1)


# --- metric extraction preserves run order ---------------------------------


def test_extract_metrics_keeps_run_order():
    """The method walks the values in the order they were produced."""
    runs = [{"a": 1.0, "_replication_info": {}}, {"a": 2.0}, {"a": 3.0}]

    assert extract_metrics(runs) == {"a": [1.0, 2.0, 3.0]}


# --- wired into run_replications -------------------------------------------


def test_run_replications_reports_precision():
    out = run_replications(
        until=25, replications=8, model=_build_stochastic_line(), n_jobs=1
    )

    entry = out["precision"]["metrics"]["snk.num_item_received"]
    assert entry["reached"] in (True, False)
    assert out["precision"]["settings"]["desired_precision"] == 0.10
    json.dumps(out)


def test_run_replications_rejects_a_bad_target_before_running():
    with pytest.raises(ValueError, match="desired_precision"):
        run_replications(
            until=25,
            replications=8,
            model=_build_stochastic_line(),
            desired_precision=2.0,
        )


def test_run_replications_tables_sink_throughput_by_default():
    """The metric a study is about, without the caller having to name a key."""
    out = run_replications(
        until=25, replications=8, model=_build_stochastic_line(), n_jobs=1
    )
    metrics = out["precision"]["metrics"]

    assert "table" in metrics["snk.num_item_received"]
    assert all(
        "table" not in entry
        for name, entry in metrics.items()
        if name != "snk.num_item_received"
    )


def test_run_replications_empty_list_asks_for_no_tables():
    """An empty list is a choice, not an absent argument."""
    out = run_replications(
        until=25,
        replications=8,
        model=_build_stochastic_line(),
        precision_tables_for=[],
        n_jobs=1,
    )

    assert all(
        "table" not in entry for entry in out["precision"]["metrics"].values()
    )


# --- picking the metrics that matter ---------------------------------------


def test_sink_throughput_metrics_names_every_sink():
    m = _build_stochastic_line()
    create_sink("snk2", model=m)

    assert sink_throughput_metrics(m.spec) == [
        "snk.num_item_received",
        "snk2.num_item_received",
    ]


def test_sink_throughput_metrics_empty_without_a_sink():
    m = FactoryModel()
    create_source("src", inter_arrival_time="exp(1)", model=m)

    assert sink_throughput_metrics(m.spec) == []

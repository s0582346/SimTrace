"""Tests for run_replications and the ReplicationAnalyzer.

The replication loop is driven by a build_model factory returning a fresh
FactoryModel per run; here that factory is a small stochastic line so the runs
actually differ across seeds.
"""

import json

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import create_buffer, create_sink, create_source
from simtrace.tools.simulation import connect, run_replications
from simtrace.tools.simulation.replication_analysis import ReplicationAnalyzer


def _build_stochastic_line() -> FactoryModel:
    """A freshly built line whose arrivals are random draws (source->buf->sink)."""
    m = FactoryModel()
    create_source("src", inter_arrival_time="exp(1)", blocking=True, model=m)
    create_sink("snk", model=m)
    create_buffer("buf", capacity=4, model=m)
    connect("buf", "src", "snk", model=m)
    return m


# --- parameter validation --------------------------------------------------


def test_replications_below_two_rejected():
    with pytest.raises(ValueError, match="between 2 and 100"):
        run_replications(_build_stochastic_line, until=20, replications=1)


def test_replications_above_hundred_rejected():
    with pytest.raises(ValueError, match="between 2 and 100"):
        run_replications(_build_stochastic_line, until=20, replications=101)


def test_replications_bool_rejected():
    # bool is an int subclass; True must not be accepted as a count of 1.
    with pytest.raises(ValueError, match="replications must be an int"):
        run_replications(_build_stochastic_line, until=20, replications=True)


def test_replications_non_int_rejected():
    with pytest.raises(ValueError, match="replications must be an int"):
        run_replications(_build_stochastic_line, until=20, replications=3.0)


def test_until_non_positive_rejected():
    with pytest.raises(ValueError, match="until must be > 0"):
        run_replications(_build_stochastic_line, until=0, replications=3)


def test_seed_base_non_int_rejected():
    with pytest.raises(ValueError, match="random_seed_base must be an int"):
        run_replications(
            _build_stochastic_line, until=20, replications=3, random_seed_base=1.5
        )


# --- the loop --------------------------------------------------------------


def test_runs_all_replications_and_reports_counts():
    out = run_replications(_build_stochastic_line, until=30, replications=4)
    assert out["requested_replications"] == 4
    assert out["successful_replications"] == 4
    assert out["failures"] == []


def test_replication_info_records_number_seed_timestamp():
    base = 500
    out = run_replications(
        _build_stochastic_line, until=30, replications=3, random_seed_base=base
    )
    infos = [
        r["_replication_info"]
        for r in out["analysis"]["_individual_replications"]
    ]
    assert [info["replication"] for info in infos] == [0, 1, 2]
    # Seeds are spread out by i * 1000 from the base.
    assert [info["seed"] for info in infos] == [base, base + 1000, base + 2000]
    for info in infos:
        assert isinstance(info["timestamp"], str) and info["timestamp"]


def test_same_seed_base_reproduces_batch():
    first = run_replications(
        _build_stochastic_line, until=40, replications=3, random_seed_base=7
    )
    second = run_replications(
        _build_stochastic_line, until=40, replications=3, random_seed_base=7
    )
    # Metric means match across the two identically-seeded batches.
    fa = first["analysis"]
    sa = second["analysis"]
    metric_keys = [k for k in fa if not k.startswith("_")]
    assert metric_keys  # there is at least one numeric metric to compare
    for key in metric_keys:
        assert fa[key]["mean"] == sa[key]["mean"]


def test_different_seed_base_diverges():
    first = run_replications(
        _build_stochastic_line, until=40, replications=3, random_seed_base=1
    )
    second = run_replications(
        _build_stochastic_line, until=40, replications=3, random_seed_base=9999
    )
    metric = "snk.num_item_received"
    assert (
        first["analysis"][metric]["mean"] != second["analysis"][metric]["mean"]
    )


# --- failure handling ------------------------------------------------------


def test_one_bad_run_does_not_kill_the_batch():
    """A factory that fails on its 2nd invocation still yields a valid batch."""
    calls = {"n": 0}

    def flaky_build() -> FactoryModel:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom on build")
        return _build_stochastic_line()

    out = run_replications(flaky_build, until=30, replications=4)
    assert out["requested_replications"] == 4
    assert out["successful_replications"] == 3
    assert len(out["failures"]) == 1
    failure = out["failures"][0]
    assert failure["replication"] == 1
    assert failure["seed"] == 1000  # base 0 + 1 * 1000
    assert "boom on build" in failure["error"]


def test_fewer_than_two_successes_raises():
    """If almost every run fails, the <2-success guard fires."""

    def always_fail() -> FactoryModel:
        raise RuntimeError("nope")

    with pytest.raises(ValueError, match="need at least 2 successful runs"):
        run_replications(always_fail, until=30, replications=3)


# --- analysis output -------------------------------------------------------


def test_analysis_contains_expected_metric_and_ci():
    out = run_replications(_build_stochastic_line, until=40, replications=5)
    metric = out["analysis"]["snk.num_item_received"]
    assert metric["sample_size"] == 5
    assert "ci_95" in metric["confidence_intervals"]
    ci95 = metric["confidence_intervals"]["ci_95"]
    assert ci95["lower"] <= metric["mean"] <= ci95["upper"]


def test_industry_summary_has_report_header_and_metric():
    out = run_replications(_build_stochastic_line, until=40, replications=4)
    summary = out["summary"]
    assert "SIMULATION REPLICATION ANALYSIS SUMMARY" in summary
    assert "(95%)" in summary
    assert "[n=4]" in summary


def test_result_is_json_serializable():
    out = run_replications(_build_stochastic_line, until=30, replications=3)
    assert json.loads(json.dumps(out)) == out


# --- ReplicationAnalyzer directly ------------------------------------------


def test_analyzer_requires_two_replications():
    analyzer = ReplicationAnalyzer()
    with pytest.raises(ValueError, match="At least 2 replications"):
        analyzer.analyze_replications([{"throughput": 10.0}])


def test_analyzer_skips_metadata_and_non_numeric():
    analyzer = ReplicationAnalyzer()
    reps = [
        {"throughput": 10.0, "_replication_info": {"seed": 1}, "label": "a"},
        {"throughput": 12.0, "_replication_info": {"seed": 2}, "label": "b"},
    ]
    analysis = analyzer.analyze_replications(reps)
    assert "throughput" in analysis
    assert "label" not in analysis  # non-numeric string skipped
    assert analysis["throughput"]["mean"] == 11.0


def test_analyzer_computes_t_ci_half_width():
    analyzer = ReplicationAnalyzer()
    reps = [{"x": float(v)} for v in (10, 12, 14, 11, 13)]
    analysis = analyzer.analyze_replications(reps)
    metric = analysis["x"]
    assert metric["mean"] == 12.0
    # Half-width is positive and the 95% interval brackets the mean.
    ci95 = metric["confidence_intervals"]["ci_95"]
    assert ci95["half_width"] > 0
    assert ci95["lower"] < 12.0 < ci95["upper"]

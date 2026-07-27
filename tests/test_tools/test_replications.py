"""Tests for run_replications and the ReplicationAnalyzer.

run_replications replicates an already-assembled model by replaying its recorded
build spec once per run. Here that model is a small stochastic line so the runs
actually differ across seeds.
"""

import json

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import create_buffer, create_sink, create_source
from simtrace.tools.simulation import connect, run_replications, run_simulation
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
        run_replications(until=20, replications=1, model=_build_stochastic_line())


def test_replications_above_hundred_rejected():
    with pytest.raises(ValueError, match="between 2 and 100"):
        run_replications(until=20, replications=101, model=_build_stochastic_line())


def test_replications_bool_rejected():
    # bool is an int subclass; True must not be accepted as a count of 1.
    with pytest.raises(ValueError, match="replications must be an int"):
        run_replications(until=20, replications=True, model=_build_stochastic_line())


def test_replications_non_int_rejected():
    with pytest.raises(ValueError, match="replications must be an int"):
        run_replications(until=20, replications=3.0, model=_build_stochastic_line())


def test_until_non_positive_rejected():
    with pytest.raises(ValueError, match="until must be > 0"):
        run_replications(until=0, replications=3, model=_build_stochastic_line())


def test_seed_base_non_int_rejected():
    with pytest.raises(ValueError, match="random_seed_base must be an int"):
        run_replications(
            until=20,
            replications=3,
            random_seed_base=1.5,
            model=_build_stochastic_line(),
        )


def test_empty_model_rejected():
    with pytest.raises(ValueError, match="The model is empty"):
        run_replications(until=30, replications=3, model=FactoryModel())


# --- the loop --------------------------------------------------------------


def test_runs_all_replications_and_reports_counts():
    out = run_replications(until=30, replications=4, model=_build_stochastic_line())
    assert out["requested_replications"] == 4
    assert out["successful_replications"] == 4
    assert out["failures"] == []


def test_replication_info_records_number_seed_timestamp():
    base = 500
    out = run_replications(
        until=30,
        replications=3,
        random_seed_base=base,
        model=_build_stochastic_line(),
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
        until=40, replications=3, random_seed_base=7, model=_build_stochastic_line()
    )
    second = run_replications(
        until=40, replications=3, random_seed_base=7, model=_build_stochastic_line()
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
        until=40, replications=3, random_seed_base=1, model=_build_stochastic_line()
    )
    second = run_replications(
        until=40,
        replications=3,
        random_seed_base=9999,
        model=_build_stochastic_line(),
    )
    metric = "snk.num_item_received"
    assert (
        first["analysis"][metric]["mean"] != second["analysis"][metric]["mean"]
    )


# --- failure handling ------------------------------------------------------


def _fail_rebuild_on(monkeypatch, should_fail) -> None:
    """Make the per-run rebuild raise for the replications `should_fail` picks.

    Patches the rebuild seam rather than the model, since that is where a
    per-replication failure can originate now that there is no factory.
    """
    import simtrace.tools.simulation.replications as mod

    real = mod.build_from_spec
    calls = {"n": 0}

    def flaky(spec):
        i = calls["n"]
        calls["n"] += 1
        if should_fail(i):
            raise RuntimeError("boom on build")
        return real(spec)

    monkeypatch.setattr(mod, "build_from_spec", flaky)


def test_one_bad_run_does_not_kill_the_batch(monkeypatch):
    """A run that fails is logged and the batch carries on."""
    _fail_rebuild_on(monkeypatch, lambda i: i == 1)

    out = run_replications(until=30, replications=4, model=_build_stochastic_line())
    assert out["requested_replications"] == 4
    assert out["successful_replications"] == 3
    assert len(out["failures"]) == 1
    failure = out["failures"][0]
    assert failure["replication"] == 1
    assert failure["seed"] == 1000  # base 0 + 1 * 1000
    assert "boom on build" in failure["error"]


def test_fewer_than_two_successes_raises(monkeypatch):
    """If almost every run fails, the <2-success guard fires."""
    _fail_rebuild_on(monkeypatch, lambda i: True)

    with pytest.raises(ValueError, match="need at least 2 successful runs"):
        run_replications(until=30, replications=3, model=_build_stochastic_line())


# --- independence from the source model ------------------------------------


def test_runs_are_independent_samples():
    """Each run is its own sample, not a repeat or a running total.

    Re-running one model instead would raise on replication 1 (SimPy refuses an
    `until` at or below the clock); if `until` were advanced to dodge that, the
    shared stats objects would make each value a cumulative total, i.e. strictly
    increasing. Neither shape should appear here.
    """
    out = run_replications(until=60, replications=5, model=_build_stochastic_line())
    received = [
        rep["snk.num_item_received"]
        for rep in out["analysis"]["_individual_replications"]
    ]
    assert out["failures"] == [], f"runs failed: {out['failures']}"
    assert len(set(received)) > 1, f"all replications identical: {received}"
    assert all(v > 0 for v in received), f"empty runs: {received}"
    monotonic = all(a < b for a, b in zip(received, received[1:]))
    assert not monotonic, f"looks cumulative, not per-run: {received}"


def test_leaves_the_source_model_untouched():
    """The source model is read for its spec only — run state must survive."""
    m = _build_stochastic_line()
    run_simulation(25, seed=5, model=m)
    before_now = m.env.now
    before_paths = dict(m.item_paths)
    before_events = len(m.events)

    run_replications(until=40, replications=3, model=m)

    # Clock, plus the last run's captured flow that the verify_* tools replay.
    assert m.env.now == before_now
    assert m.item_paths == before_paths
    assert len(m.events) == before_events


def test_works_on_an_already_run_model():
    # The clock being spent must not matter: each replication rebuilds.
    m = _build_stochastic_line()
    run_simulation(30, seed=2, model=m)
    out = run_replications(until=30, replications=3, model=m)
    assert out["successful_replications"] == 3
    received = [
        rep["snk.num_item_received"]
        for rep in out["analysis"]["_individual_replications"]
    ]
    assert all(v > 0 for v in received)


def test_later_edits_do_not_affect_a_running_batch():
    """The spec is snapshotted up front, so a mid-batch edit can't leak in."""
    m = _build_stochastic_line()
    out = run_replications(until=30, replications=3, model=m)
    # A node added afterwards is absent from every replication's metrics.
    create_sink("added_later", model=m)
    assert not any(k.startswith("added_later.") for k in out["analysis"])


# --- analysis output -------------------------------------------------------


def test_analysis_contains_expected_metric_and_ci():
    out = run_replications(until=40, replications=5, model=_build_stochastic_line())
    metric = out["analysis"]["snk.num_item_received"]
    assert metric["sample_size"] == 5
    assert "ci_95" in metric["confidence_intervals"]
    ci95 = metric["confidence_intervals"]["ci_95"]
    assert ci95["lower"] <= metric["mean"] <= ci95["upper"]


def test_industry_summary_has_report_header_and_metric():
    out = run_replications(until=40, replications=4, model=_build_stochastic_line())
    summary = out["summary"]
    assert "SIMULATION REPLICATION ANALYSIS SUMMARY" in summary
    assert "(95%)" in summary
    assert "[n=4]" in summary


def test_result_is_json_serializable():
    out = run_replications(until=30, replications=3, model=_build_stochastic_line())
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

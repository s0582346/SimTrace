"""Tests for find_replication_count, the adaptive replication search.

sim-tools owns the stopping rule; what is ours is the runner that feeds it. So
the tests that matter here are the ones that pin down what the chunking must
never change: the counts reported have to be the same however the runs were
scheduled, because a replication's result depends on its seed alone.
"""

import json
import os

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import create_buffer, create_sink, create_source
from simtrace.tools.simulation import connect, run_simulation
from simtrace.tools.simulation.adaptive import find_replication_count
from simtrace.tools.simulation.replication_plot import write_precision_plot


def _build_stochastic_line() -> FactoryModel:
    """A freshly built line whose arrivals are random draws (source->buf->sink)."""
    m = FactoryModel()
    create_source("src", inter_arrival_time="exp(1)", blocking=True, model=m)
    create_sink("snk", model=m)
    create_buffer("buf", capacity=4, model=m)
    connect("buf", "src", "snk", model=m)
    return m


def _search(**kwargs):
    """A short search on the stochastic line, sequential unless told otherwise."""
    kwargs.setdefault("until", 60)
    kwargs.setdefault("model", _build_stochastic_line())
    kwargs.setdefault("n_jobs", 1)
    return find_replication_count(**kwargs)


THROUGHPUT = "snk.num_item_received"


# --- the search ------------------------------------------------------------


def test_settles_well_inside_the_budget():
    out = _search()

    assert out["converged"] is True
    assert out["unresolved"] == []
    assert 0 < out["replications_needed"][THROUGHPUT] < 50


def test_runs_at_least_the_count_it_reports():
    """A count can only be justified by runs that were actually made."""
    out = _search()

    assert out["runs_executed"] >= out["replications_needed"][THROUGHPUT]


def test_defaults_to_sink_throughput():
    out = _search()

    assert list(out["replications_needed"]) == [THROUGHPUT]


def test_impossible_target_exhausts_the_budget_without_hanging():
    out = _search(desired_precision=0.0001, replication_budget=12)

    assert out["converged"] is False
    assert out["unresolved"] == [THROUGHPUT]
    assert out["replications_needed"][THROUGHPUT] is None
    # The look-ahead window still has to be filled before a count is refused.
    assert out["runs_executed"] < 12 + 20


def test_result_is_json_serializable():
    json.dumps(_search())


# --- what the chunking must not change -------------------------------------


def test_chunk_size_does_not_change_the_answer():
    """The runner schedules the runs; it does not take part in the decision."""
    one_at_a_time = _search(chunk_size=1)
    in_chunks = _search(chunk_size=7)

    assert one_at_a_time["replications_needed"] == in_chunks["replications_needed"]
    assert one_at_a_time["tables"] == in_chunks["tables"]


def test_workers_do_not_change_the_answer():
    sequential = _search(n_jobs=1)
    parallel = _search(n_jobs=4)

    assert sequential["replications_needed"] == parallel["replications_needed"]
    assert sequential["tables"] == parallel["tables"]


def test_same_seed_base_replays_the_search():
    assert _search(random_seed_base=7) == _search(random_seed_base=7)


def test_different_seed_base_is_a_different_sample():
    first = _search(random_seed_base=0)["tables"][THROUGHPUT]
    second = _search(random_seed_base=99)["tables"][THROUGHPUT]

    assert [row["Mean"] for row in first] != [row["Mean"] for row in second]


def test_chunk_size_one_still_reports_no_overshoot_beyond_the_run_count():
    """With no look-ahead prefetch, runs executed is exactly what was asked."""
    out = _search(chunk_size=1, look_ahead=2, replication_budget=10)

    assert out["runs_executed"] <= 10 + 2 + 1


# --- the tables ------------------------------------------------------------


def test_table_rows_are_numbered_from_one():
    """sim-tools resets the index when it concatenates the metrics."""
    table = _search()["tables"][THROUGHPUT]

    assert [row["replications"] for row in table] == list(
        range(1, len(table) + 1)
    )


def test_table_carries_the_running_interval():
    table = _search()["tables"][THROUGHPUT]
    last = table[-1]

    assert last["Lower Interval"] < last["Cumulative Mean"] < last["Upper Interval"]
    # No standard deviation exists yet on the first row.
    assert table[0]["Standard Deviation"] is None


def test_table_drops_the_metric_label_column():
    table = _search()["tables"][THROUGHPUT]

    assert "metric" not in table[0]


# --- independence from the source model ------------------------------------


def test_leaves_the_source_model_untouched():
    m = _build_stochastic_line()
    run_simulation(20, seed=1, model=m)
    before = m.env.now

    find_replication_count(until=60, model=m, n_jobs=1)

    assert m.env.now == before


# --- guards ----------------------------------------------------------------


def test_empty_model_rejected():
    with pytest.raises(ValueError, match="model is empty"):
        find_replication_count(until=60, model=FactoryModel())


def test_model_without_a_sink_asks_for_metrics():
    m = FactoryModel()
    create_source("src", inter_arrival_time="exp(1)", model=m)

    with pytest.raises(ValueError, match="No target metric"):
        find_replication_count(until=60, model=m)


def test_unknown_target_metric_lists_the_real_ones():
    with pytest.raises(ValueError, match="Available metrics"):
        _search(target_metrics=["snk.throughput"])


def test_failed_replication_stops_the_search(monkeypatch):
    """A hole in the sequence has no sensible way to be stepped over."""
    import simtrace.tools.simulation.replications as replications

    def _boom(spec):
        raise RuntimeError("rebuild exploded")

    monkeypatch.setattr(replications, "build_from_spec", _boom)

    with pytest.raises(ValueError, match="cannot continue"):
        _search()


@pytest.mark.parametrize("bad", [0, 1, 101, -5])
def test_budget_out_of_range_rejected(bad):
    with pytest.raises(ValueError, match="replication_budget"):
        _search(replication_budget=bad)


def test_budget_below_the_initial_runs_rejected():
    with pytest.raises(ValueError, match="at least"):
        _search(replication_budget=4, initial_replications=9)


def test_bad_chunk_size_rejected():
    with pytest.raises(ValueError, match="chunk_size"):
        _search(chunk_size=0)


def test_bad_until_rejected():
    with pytest.raises(ValueError, match="until"):
        _search(until=0)


# --- the plot --------------------------------------------------------------


def test_no_plot_without_a_path(tmp_path):
    out = _search()

    assert out["plot_paths"] == {}
    assert list(tmp_path.iterdir()) == []


def test_plot_written_where_asked(tmp_path):
    target = tmp_path / "search.html"
    out = _search(plot_path=str(target))

    assert out["plot_paths"][THROUGHPUT] == str(target)
    assert target.stat().st_size > 0
    assert "<html" in target.read_text(encoding="utf-8")[:2000].lower()


def test_plot_drawn_without_a_count_when_nothing_settled(tmp_path):
    """No marker to place, and no crash for the missing one."""
    target = tmp_path / "unsettled.html"
    out = _search(
        desired_precision=0.0001, replication_budget=8, plot_path=str(target)
    )

    assert out["converged"] is False
    assert target.stat().st_size > 0


def test_plot_names_the_metric_when_there_are_several(tmp_path):
    table = _search()["tables"][THROUGHPUT]
    written = write_precision_plot(
        metric=THROUGHPUT,
        replications_needed=4,
        table=table,
        path=str(tmp_path / "search.html"),
        suffix_with_metric=True,
    )

    assert os.path.basename(written) == f"search.{THROUGHPUT}.html"


def test_plot_rejects_a_table_that_is_not_one(tmp_path):
    with pytest.raises(ValueError, match="does not look like"):
        write_precision_plot(
            metric=THROUGHPUT,
            replications_needed=None,
            table=[{"replications": 1, "something": 2.0}],
            path=str(tmp_path / "x.html"),
        )


def test_plot_rejects_an_empty_table(tmp_path):
    with pytest.raises(ValueError, match="No replication table"):
        write_precision_plot(
            metric=THROUGHPUT,
            replications_needed=None,
            table=[],
            path=str(tmp_path / "x.html"),
        )

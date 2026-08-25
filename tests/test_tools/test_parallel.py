"""Tests for the worker-count decision behind parallel replications.

Two questions, tested separately: how many workers the machine allows
(`worker_budget`), and whether a batch is worth spreading (`choose_workers`).
No test here starts a process; the end-to-end run is in test_replications.py.
"""

import sys

import pytest

from simtrace.tools.simulation import parallel


# --- detecting what the machine allows -------------------------------------


def test_detect_cores_is_at_least_one():
    assert parallel.detect_cores() >= 1


def test_budget_is_half_the_cores_capped_at_max(monkeypatch):
    monkeypatch.setattr(parallel, "detect_cores", lambda: 22)
    # Half of 22 is 11, but the measured ceiling is lower.
    assert parallel.worker_budget() == parallel.MAX_WORKERS

    monkeypatch.setattr(parallel, "detect_cores", lambda: 8)
    assert parallel.worker_budget() == 4


def test_budget_never_drops_below_one(monkeypatch):
    """A one- or two-core machine still gets a usable answer."""
    for cores in (1, 2):
        monkeypatch.setattr(parallel, "detect_cores", lambda cores=cores: cores)
        assert parallel.worker_budget() == 1


def test_explicit_n_jobs_is_taken_at_face_value(monkeypatch):
    monkeypatch.setattr(parallel, "detect_cores", lambda: 22)
    assert parallel.worker_budget(1) == 1
    assert parallel.worker_budget(3) == 3
    # Above the automatic cap: the caller asked for it.
    assert parallel.worker_budget(12) == 12
    # joblib's convention for "every core".
    assert parallel.worker_budget(parallel.ALL_CORES) == 22


def test_bad_n_jobs_rejected():
    with pytest.raises(ValueError, match="n_jobs must be >= 1"):
        parallel.worker_budget(0)
    with pytest.raises(ValueError, match="n_jobs must be >= 1"):
        parallel.worker_budget(-2)
    with pytest.raises(ValueError, match="n_jobs must be an int"):
        parallel.worker_budget(2.0)
    # bool is an int subclass; True must not be read as a worker count of 1.
    with pytest.raises(ValueError, match="n_jobs must be an int"):
        parallel.worker_budget(True)


def test_env_var_caps_every_path(monkeypatch):
    monkeypatch.setattr(parallel, "detect_cores", lambda: 22)
    monkeypatch.setenv(parallel.MAX_WORKERS_ENV, "2")
    assert parallel.worker_budget() == 2
    assert parallel.worker_budget(16) == 2  # caps an explicit request too
    assert parallel.worker_budget(parallel.ALL_CORES) == 2


def test_env_var_can_force_sequential(monkeypatch):
    monkeypatch.setattr(parallel, "detect_cores", lambda: 22)
    monkeypatch.setenv(parallel.MAX_WORKERS_ENV, "1")
    assert parallel.worker_budget() == 1


def test_blank_env_var_is_ignored(monkeypatch):
    monkeypatch.setattr(parallel, "detect_cores", lambda: 8)
    monkeypatch.setenv(parallel.MAX_WORKERS_ENV, "  ")
    assert parallel.worker_budget() == 4


def test_unusable_env_var_raises(monkeypatch):
    """A typo must raise, not fall back to the default budget."""
    for bad in ("nope", "0", "-4"):
        monkeypatch.setenv(parallel.MAX_WORKERS_ENV, bad)
        with pytest.raises(ValueError, match=parallel.MAX_WORKERS_ENV):
            parallel.worker_budget()


# --- deciding whether the batch is worth spreading -------------------------

COLD = parallel.COLD_POOL_SECONDS


def test_cheap_batch_stays_sequential():
    """The test-suite model: milliseconds a run, against a 2 s pool."""
    assert parallel.choose_workers(3, 0.0018, 8, startup_seconds=COLD) == 1


def test_expensive_batch_goes_parallel():
    """The production scenario: 123 runs at ~0.7 s each."""
    assert parallel.choose_workers(123, 0.7, 8, startup_seconds=COLD) == 8


def test_workers_never_exceed_the_work_left():
    assert parallel.choose_workers(3, 5.0, 8, startup_seconds=COLD) == 3


def test_single_worker_budget_stays_sequential():
    """One worker is the sequential path plus a pool, at any batch size."""
    assert parallel.choose_workers(500, 10.0, 1, startup_seconds=COLD) == 1


def test_nothing_pending_stays_sequential():
    assert parallel.choose_workers(1, 10.0, 8, startup_seconds=COLD) == 1
    assert parallel.choose_workers(0, 10.0, 8, startup_seconds=COLD) == 1


def test_unmeasurable_replication_stays_sequential():
    """A zero timing means the probe measured nothing."""
    assert parallel.choose_workers(50, 0.0, 8, startup_seconds=COLD) == 1


def test_a_warm_pool_makes_a_marginal_batch_worth_it():
    """The same batch can flip once the executor is up.

    loky keeps its workers alive between batches, so a second call in the same
    session is priced against a pool that costs almost nothing to reuse.
    """
    pending, each = 12, 0.25
    cold = parallel.choose_workers(pending, each, 8, startup_seconds=COLD)
    warm = parallel.choose_workers(
        pending, each, 8, startup_seconds=parallel.WARM_POOL_SECONDS
    )
    assert cold == 1
    assert warm == 8


# --- keeping worker narration off the protocol channel ---------------------


def test_silence_stdout_is_a_no_op_in_the_main_process(capsys):
    """Only a worker's stdout is discarded. The main process keeps its own."""
    before = sys.stdout
    parallel.silence_stdout()
    assert sys.stdout is before

    print("still visible")
    assert capsys.readouterr().out == "still visible\n"


def test_describe_reports_the_route_taken():
    assert parallel.describe(1)["mode"] == "sequential"
    assert parallel.describe(4)["mode"] == "parallel"
    assert parallel.describe(4)["workers"] == 4
    assert parallel.describe(4)["cores_detected"] >= 1

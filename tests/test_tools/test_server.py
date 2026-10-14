"""Tests for the MCP server's stdin handling."""

import subprocess
import sys
import textwrap
import threading

# Runs in a child whose stdin is a pipe, like the MCP server. A thread blocks
# reading sys.stdin while a worker pool starts; then the pipe is written.
_CHILD = textwrap.dedent(
    """
    import sys, threading, time
    from joblib import Parallel, delayed
    from simtrace.server import _private_stdin

    def square(x):
        return x * x

    _private_stdin()
    got = []
    reader = threading.Thread(target=lambda: got.append(sys.stdin.readline()))
    reader.start()
    time.sleep(0.5)
    total = sum(Parallel(n_jobs=2, backend="loky", timeout=30)(
        delayed(square)(i) for i in range(10)))
    print("pool", total, flush=True)
    reader.join(30)
    print("read", got, flush=True)
    """
)


def test_worker_pool_starts_while_stdin_is_being_read():
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Written only once the pool has finished: the reader blocks until then.
    try:
        lines = []
        waiter = threading.Thread(
            target=lambda: lines.append(child.stdout.readline()), daemon=True
        )
        waiter.start()
        waiter.join(60)
        assert lines == ["pool 285\n"], "the worker pool did not start"
        child.stdin.write("request\n")
        child.stdin.flush()
        out, err = child.communicate(timeout=60)
    finally:
        child.kill()

    assert "read ['request\\n']" in out, err

"""Run-lifecycle tools: advance the simulation clock and reset the model.

`run_simulation` executes the assembled model up to a cutoff and returns per-
node/edge stats; `reset_model` discards the session graph and restarts the clock
at 0. `run_simulation` also captures this run's item-flow events and per-item
node paths onto the model for the validation tools to replay.

Conventions (see architecture/simulation_tools.md):
  - fail with friendly ValueErrors, not raw KeyErrors,
  - FactorySimPy prints to stdout during run; that is suppressed here so it can't
    corrupt the MCP stdio (JSON-RPC) channel.
"""

from __future__ import annotations

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.model import reset_model as reset_session_model
from simtrace.tools.telemetry import traced_stdout
from simtrace.tools.utils import require_positive_number


def reset_model() -> dict:
    """Discard the current session graph and start a fresh, empty one.

    Replaces the session model with a brand-new one: every node and edge is
    dropped and the simulation clock restarts at 0 (a new `simpy.Environment`).
    Use this to recover from a dirty session — leftover components from earlier
    work, an orphaned node that can't be wired or deleted individually, or a
    clock that has already advanced past the `until` you want to run to.

    Unlike the other lifecycle tools, this always operates on the shared session
    model (there is nothing to reset in a caller-supplied one).

    Returns a summary of what was cleared and the reset clock.
    """
    old = get_session_model()
    cleared_nodes = len(old.nodes)
    cleared_edges = len(old.edges)

    reset_session_model()

    return {
        "cleared_nodes": cleared_nodes,
        "cleared_edges": cleared_edges,
        "now": 0,
    }


def run_simulation(
    until: float,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Run the simulation up to time `until` and return a stats summary.

    This is where FactorySimPy's scheduled `behaviour()` processes actually
    execute, so any unmet edge-cardinality requirement (e.g. a Source with no
    out_edge) surfaces here as an AssertionError. Run `validate_model` first to
    catch those as friendly errors.
    Args:
        until: simulation end time; must be a positive number.

    Returns a summary dict with the end time and per-node/edge stats.
    """
    model = model if model is not None else get_session_model()

    require_positive_number("until", until)

    # Capture this run's item-flow events and per-item node paths onto the model for the validation tools
    model.events.clear()
    model.item_paths.clear()
    with traced_stdout(collector=model.events, paths=model.item_paths):
        model.env.run(until=until)

    nodes = {
        node_id: getattr(node, "stats", None)
        for node_id, node in model.nodes.items()
    }
    edges = {
        edge_id: getattr(edge, "stats", None)
        for edge_id, edge in model.edges.items()
    }
    return {
        "until": until,
        "now": model.env.now,
        "nodes": nodes,
        "edges": edges,
    }

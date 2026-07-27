"""Lifecycle tools that operate on the assembled model as a whole.

Where the `create_*` builders in `simtrace.tools.nodes` / `simtrace.tools.edges`
populate the session model with components, these tools work on the graph as a
whole, split across two submodules:

  - `graph.connect` / `graph.get_model` — wire the components together and
    inspect the resulting graph.
  - `lifecycle.run_simulation` / `lifecycle.reset_model` — run the simulation
    clock and reset the model.
  - `replications.run_replications` — run the assembled model many times and
    report statistics (means, confidence intervals) across the runs, rebuilding
    it from its recorded build spec (`rebuild.build_from_spec`) once per run.

See architecture/simulation_tools.md for the tools' conventions. This package
re-exports them all so `from simtrace.tools.simulation import connect, ...` and
`simulation.connect` keep working as a single tool surface.
"""

from __future__ import annotations

from simtrace.tools.simulation.graph import connect, get_model
from simtrace.tools.simulation.lifecycle import reset_model, run_simulation
from simtrace.tools.simulation.rebuild import build_from_spec
from simtrace.tools.simulation.replications import run_replications

__all__ = [
    "connect",
    "get_model",
    "reset_model",
    "run_simulation",
    "run_replications",
    "build_from_spec",
]

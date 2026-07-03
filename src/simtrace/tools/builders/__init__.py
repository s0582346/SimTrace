"""Component builders: the `create_*` functions that populate the model.

Each builder validates its arguments, instantiates a FactorySimPy component
bound to the shared model environment, registers it, and returns a small
JSON-serializable summary. The MCP server wraps these as tools; tests call them
directly. Components start unwired — `simgen.tools.simulation.connect` wires
them later.

  - `nodes` — the active components (Source, Sink, Machine, Splitter, Combiner)
    that generate, process, and consume flow items.
  - `edges` — the passive components (Buffer, Conveyor, Fleet) that hold and
    move flow items between two nodes.

This package re-exports every builder so `from simgen.tools.builders import
create_source, ...` and `builders.create_source` work as a single surface.
See architecture/node_tools.md and architecture/edge_tools.md.
"""

from __future__ import annotations

from simgen.tools.builders.edges import (
    create_buffer,
    create_conveyor,
    create_fleet,
)
from simgen.tools.builders.nodes import (
    create_combiner,
    create_machine,
    create_sink,
    create_source,
    create_splitter,
)

__all__ = [
    "create_buffer",
    "create_combiner",
    "create_conveyor",
    "create_fleet",
    "create_machine",
    "create_sink",
    "create_source",
    "create_splitter",
]
